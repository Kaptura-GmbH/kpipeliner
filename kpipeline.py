#!/usr/bin/env python3
"""
kpl (kpipeliner) – CLI entry point.

Usage:
    kpl init
    kpl gui
    kpl list
    kpl add <node_type> [<node_id>]
    kpl run [<node_id>] [--params key=val ...]
    kpl run_debug [<node_id>] [--params key=val ...]
    kpl tree
    kpl status
    kpl clean [<node_id>]
    kpl group list
    kpl group clean <group_id>
    kpl group run <group_id> [--no-use-cache]
    kpl node_env install [--node_type TYPE] [--category CATEGORY]
    kpl base_node create
    kpl base_node install [--node_name NAME]
    kpl base_node uninstall <node_name>
"""

import argparse
import importlib.util
import json
import os
import sys
import shutil
import subprocess
import threading
import time
import uuid as _uuid_mod

PIPELINER_DIR = os.path.dirname(os.path.abspath(__file__))


def _configured_venv_dir():
    """The venv dir configured in config.yaml's `python_env` (set by
    install.sh --venv), or None to use the default python environment
    (whatever python invoked kpl). Parsed by hand instead of with `yaml`:
    this runs before we know the CURRENT interpreter even has yaml installed
    — that's exactly the case re-execing into the configured venv fixes."""
    cfg_path = os.path.join(PIPELINER_DIR, 'config.yaml')
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped.startswith('python_env:'):
                    continue
                val = stripped.split(':', 1)[1].strip().strip('"\'')
                if val in ('', 'null', '~', 'None'):
                    return None
                return val
    except Exception:
        pass
    return None


def _ensure_venv_python():
    """Re-exec under the configured python_env when one is set and we're not
    already running there, so the CLI, GUI and fallback node runs are always
    consistent with it. No python_env configured -> keep whatever python
    invoked kpl (the default python environment)."""
    venv_dir = _configured_venv_dir()
    if not venv_dir:
        return  # no configured env — use the default python environment
    venv_python = os.path.join(venv_dir, 'bin', 'python')
    if not os.path.exists(venv_python):
        return  # configured env missing/broken — fall back to current python
    if os.path.realpath(sys.prefix) == os.path.realpath(venv_dir):
        return  # already running inside it
    if os.environ.get('_KPL_REEXEC') == '1':
        return  # safety: never re-exec twice
    os.environ['_KPL_REEXEC'] = '1'
    os.execv(venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:])


# Must run before any third-party import (yaml, …): with a non-venv python
# those imports could fail before we get the chance to switch interpreter.
_ensure_venv_python()

import yaml  # noqa: E402

MAX_NODES = 500

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _add_pipeliner_to_path():
    if PIPELINER_DIR not in sys.path:
        sys.path.insert(0, PIPELINER_DIR)


_add_pipeliner_to_path()

from kpipeline import KNode, KGraph, KParameter, NodeState, PipelineExecutor  # noqa: E402
from kpipeline.src.kbase_nodes import (  # noqa: E402
    get_default_base_nodes_dir, scan_base_nodes, find_node_dir,
)
from kpipeline.src.kpaths import read_configured_dirs  # noqa: E402


def _pid_alive(pid):
    """Whether a process with this pid exists (a pid we may not signal still exists)."""
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Pipeliner class
# ---------------------------------------------------------------------------

class Pipeliner:
    GRAPH_CONFIG = 'graph_config.yaml'

    def __init__(self):
        self._executed_nodes = []
        # Nodes whose cached result must NOT be reused although it exists on disk:
        # set by invalidate_for_rerun() when a kwui workflow jumps back/forward or
        # is reset, so a dependent that is pulled in again recomputes from the new
        # upstream result. Always empty outside that (so every other run path is
        # unchanged); a node leaves it once it has run again.
        self._force_rerun = set()
        # Steps a kwui workflow jumped OVER (forward jump): not pulled in as an
        # ordering-only (jump-link) predecessor of a later step; they still run if
        # a later step needs their DATA (nodes.X.Y). Empty outside kwui.
        self._skipped_steps = set()
        # Nodes currently being resolved somewhere up the run_node() call
        # stack — guards the jump-forward cascade below against re-entering a
        # node that's already in progress (e.g. a jump-successor chain that
        # loops back to a node still running further up the stack).
        self._in_progress_nodes = set()
        # Live node states, mirrored to cache/run_state.json so a GUI serving
        # the same directory can highlight nodes running in a terminal `kpl run`.
        self._node_states = {}
        self._state_lock = threading.Lock()
        self._run_source = 'gui' if os.environ.get('KPL_GUI_RUN') == '1' else 'terminal'

    # -----------------------------------------------------------------------
    # init
    # -----------------------------------------------------------------------

    def init_graph_dir(self):
        print('Initializing pipeline directory...')
        success = True

        # graph_config.yaml
        template = os.path.join(PIPELINER_DIR, 'kpipeline', 'templates', 'config.graph_template.yaml')
        dest = self.GRAPH_CONFIG
        if os.path.exists(dest):
            print(f"  '{dest}' already exists – skipping.")
        else:
            try:
                shutil.copy2(template, dest)
                print(f"  Created '{dest}'.")
            except Exception as e:
                print(f"  Error creating '{dest}': {e}")
                success = False

        # directories
        for d in ('extern', 'data', 'cache'):
            if not os.path.exists(d):
                os.makedirs(d)
                print(f"  Created '{d}/'.")
            else:
                print(f"  '{d}/' already exists.")

        # uuid
        uuid_file = os.path.join('extern', 'uuid.yaml')
        if not os.path.exists(uuid_file):
            try:
                unique = str(_uuid_mod.uuid4()).replace('-', '')[:12]
                with open(uuid_file, 'w') as f:
                    f.write(f'value: "{unique}"\n')
                print(f"  Created UUID file '{uuid_file}' (uuid={unique}).")
            except Exception as e:
                print(f"  Error creating UUID file: {e}")
                success = False

        if success:
            print('\nPipeline initialized. Edit graph_config.yaml to configure.')
        return success

    # -----------------------------------------------------------------------
    # GUI
    # -----------------------------------------------------------------------

    def run_gui(self, mcp=False):
        gui_server = os.path.join(PIPELINER_DIR, 'kpipeline_gui', 'server.py')
        if not os.path.exists(gui_server):
            print('GUI server not found. Please ensure kpipeline_gui/server.py exists.')
            return False

        if mcp and not _mcp_package_available():
            _print_mcp_missing()
            print('Continuing without MCP — launching the GUI on its own.')
            mcp = False

        cmd = [sys.executable, gui_server, '--workdir', os.getcwd()]
        if mcp:
            cmd.append('--mcp')

        print('Launching GUI...')
        proc = subprocess.Popen(cmd)
        try:
            proc.wait()
        except KeyboardInterrupt:
            print('\nShutting down GUI...')
            proc.terminate()
            proc.wait()

    def run_mcp(self):
        """Headless MCP server, no GUI/REST API at all (`kpl mcp`)."""
        mcp_server = os.path.join(PIPELINER_DIR, 'kpipeline_gui', 'mcp_server.py')
        if not os.path.exists(mcp_server):
            print('MCP server not found. Please ensure kpipeline_gui/mcp_server.py exists.')
            return False

        if not _mcp_package_available():
            _print_mcp_missing()
            return False

        proc = subprocess.Popen([sys.executable, mcp_server, '--workdir', os.getcwd()])
        try:
            proc.wait()
        except KeyboardInterrupt:
            print('\nShutting down MCP server...')
            proc.terminate()
            proc.wait()

    # -----------------------------------------------------------------------
    # List available node types
    # -----------------------------------------------------------------------

    def list_nodes(self):
        graph = KGraph(verbose=False)
        graph.load_externals()
        _load_graph_config(graph)
        node_types = graph.list_available_node_types()
        if not node_types:
            dirs = ', '.join(graph.get_base_nodes_dirs()) or graph.base_nodes_dir
            print(f'No node types available. Add nodes to: {dirs}')
            return

        from itertools import groupby
        by_cat = {}
        for nt in node_types:
            by_cat.setdefault(nt['category'], []).append(nt)

        print(f'\nAvailable node types ({len(node_types)} total):\n')
        for cat, nodes in sorted(by_cat.items()):
            print(f'  [{cat}]')
            for n in nodes:
                desc = f' – {n["description"]}' if n['description'] else ''
                print(f'    {n["type"]}{desc}')
        print()

    # -----------------------------------------------------------------------
    # Add node
    # -----------------------------------------------------------------------

    def add_node(self, node_type, node_id=None):
        graph = KGraph(verbose=False)
        graph.load_externals()
        if not _require_config():
            return False
        graph.load_from_yaml(self.GRAPH_CONFIG)

        # Verify type exists in the pipeline's effective base-nodes dirs
        # (custom_base_nodes_dirs first, then the default when enabled).
        dirs = graph.get_base_nodes_dirs()
        node_dir = find_node_dir(node_type, PIPELINER_DIR, dirs=dirs)
        if not node_dir:
            print(f"Node type '{node_type}' not found in base nodes "
                  f"({', '.join(dirs) or 'no base-nodes dirs configured'}).")
            return False
        base_cfg = os.path.join(node_dir, 'node_config.yaml')

        if len(graph.nodes) >= MAX_NODES:
            print(f'Maximum node count ({MAX_NODES}) reached.')
            return False

        if not node_id:
            count = graph.count_nodes(node_type=node_type)
            node_id = f'{node_type}_{count + 1}'

        if graph.get_node(node_id):
            print(f"Node '{node_id}' already exists.")
            return False

        node = KNode.__new__(KNode)
        node.name = node_id
        node.type = node_type
        node.state = NodeState.PENDING
        node.error = None
        node._verbose = False
        node.inputs = {}
        node.outputs = {}
        node.description = ''
        node.category = 'General'
        node.env_manager = None
        node.env_directory = '.venv'
        node.requirements = None
        node.externals = graph.externals
        node.uuid = graph.externals.get('uuid', '123456789')
        node.node_dir = node_dir
        node.gui = {'position': [0, 0], 'color': None}
        node._load_from_yaml(base_cfg)
        node.name = node_id

        graph.add_node(node)
        graph.save_to_yaml(self.GRAPH_CONFIG)
        print(f"Added node '{node_id}' (type: {node_type})")
        return True

    # -----------------------------------------------------------------------
    # Run
    # -----------------------------------------------------------------------

    def run_graph(self, parallel=True, use_cache=False, debug=False):
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_externals()
        graph.load_from_yaml(self.GRAPH_CONFIG)
        _warn_missing(graph)

        if not graph.nodes:
            print('No nodes in graph.')
            return True

        run_order = graph.get_run_order()
        if not run_order:
            print("No outputs declared in graph_config.yaml ('outputs:' is empty) — "
                  "nothing to run.\nMark at least one node as a pipeline output "
                  "(right-click it in the GUI, or add its name to 'outputs:' in "
                  "graph_config.yaml) to run it and whatever it depends on.")
            return True

        cpu_w = graph.execution.get('cpu_workers', 2)
        gpu_w = graph.execution.get('gpu_workers', 1)

        print(f'\nRunning {len(run_order)} of {len(graph.nodes)} node(s) needed for '
              f'the declared outputs  [cpu_workers={cpu_w}, gpu_workers={gpu_w}]\n')

        if parallel:
            executor = PipelineExecutor(
                graph, cpu_workers=cpu_w, gpu_workers=gpu_w,
                debug=debug,
                on_state_change=self._on_state_change,
            )
            success = executor.run(use_cache=use_cache, pipeliner=self)
            errors = executor.get_errors()
            if errors:
                print('\n--- Errors ---')
                for name, err in errors.items():
                    print(f'  {name}: {err}')
            return success
        else:
            for name in run_order:
                self._on_state_change(name, NodeState.PENDING)
            for name in run_order:
                ok = self.run_node(name, graph=graph, use_cache=use_cache, debug=debug)
                if not ok:
                    print(f"\n[FAILED] Pipeline stopped at node '{name}'.")
                    return False
            return True

    def kwui_init(self):
        """Copy the workflow UI template into the pipeline directory."""
        from kpipeline.src.kwui import WORKFLOW_FILE, workflow_path
        target = workflow_path()
        if os.path.exists(target):
            print(f'{WORKFLOW_FILE} already exists — leaving it untouched.')
            return True
        src = os.path.join(PIPELINER_DIR, 'kpipeline', 'templates',
                           'kwui_workflow_template.py')
        if not os.path.exists(src):
            print(f'Template not found: {src}')
            return False
        shutil.copy(src, target)
        print(f'Created {WORKFLOW_FILE}. Run the pipeline with: kpl run --kwui')
        return True

    def run_kwui(self, use_cache=False, debug=False, host=None, port=None,
                 open_browser=None):
        """Run the pipeline through workflow.py's kwebui UI.

        Deliberately sequential (one node at a time), so the tracker's single
        "current step" is always unambiguous — the parallel PipelineExecutor is
        not used in this mode.

        host/port/open_browser: None -> config.yaml's `kwui:` section defaults.
        """
        if not _require_config():
            return False
        from kpipeline.src.kwui import (WORKFLOW_FILE, run_with_ui, tracker_steps,
                                        workflow_path)

        if not os.path.exists(workflow_path()):
            print(f'No {WORKFLOW_FILE} in this pipeline. Create it with: kpl kwui init')
            return False

        graph = KGraph(verbose=False)
        graph.load_externals()
        graph.load_from_yaml(self.GRAPH_CONFIG)
        _warn_missing(graph)

        sequence = graph.get_kwui_sequence()
        if not sequence:
            print("No kwui sequence: mark a start node as a pipeline input "
                  "('inputs:' in graph_config.yaml) and chain the rest with "
                  "jump links.")
            return False

        ui_steps = tracker_steps(graph, sequence)
        print(f"\nRunning {len(sequence)} node(s) in kwui mode: "
              f"{' → '.join(sequence)}")
        print(f"Workflow UI shows {len(ui_steps)} step(s) (nodes with "
              f"`kwui: true`): {' → '.join(ui_steps) or '(none)'}\n")
        for name in sequence:
            self._on_state_change(name, NodeState.PENDING)

        def run_one(name):
            return self.run_node(name, graph=graph, use_cache=use_cache,
                                 debug=debug, trigger_jump_forward=False)

        server = _kwui_server_defaults()
        run_with_ui(
            run_one, sequence,
            host=host if host is not None else server['host'],
            port=port if port is not None else server['port'],
            open_browser=(open_browser if open_browser is not None
                          else server['open_browser']),
            # Debug runs want uvicorn's request log too; a normal run keeps the
            # terminal on the pipeline's own output.
            log_level='info' if debug else 'warning',
            invalidate=lambda names: self.invalidate_for_rerun(graph, names),
            skip=self.mark_skipped,
            accept=self.accept_failed,
        )
        return True

    def accept_failed(self, name):
        """A kwui workflow went on past step `name`, which had failed: for the rest of
        the run it counts as done (it will not be started again just because a later
        step reads its outputs), exactly like a step that ended with ERROR_SKIP."""
        if name not in self._executed_nodes:
            self._executed_nodes.append(name)
        self._force_rerun.discard(name)

    def mark_skipped(self, names):
        """A kwui workflow jumped forward over steps `names`: they are not run
        just because a later step comes after them (see _skipped_steps)."""
        self._skipped_steps |= set(names)

    def invalidate_for_rerun(self, graph, names):
        """A kwui workflow is about to run steps `names` AGAIN (a jump or Reset).
        Forget that they — and everything that depends on them, e.g. a helper
        node computing from a step's output — already ran, and stop
        trusting their cached results, so the second pass recomputes from the new
        upstream results instead of reusing stale ones. Nothing is deleted from
        disk (the cache is simply not used for these nodes until they ran again)."""
        dependents = {n.name: set() for n in graph.nodes}
        for node in graph.nodes:
            for dep in graph.get_dependencies(node.name):
                if dep in dependents:
                    dependents[dep].add(node.name)
        stale, queue = set(), list(names)
        while queue:
            current = queue.pop()
            if current in stale:
                continue
            stale.add(current)
            queue.extend(dependents.get(current, ()))
        self._executed_nodes[:] = [n for n in self._executed_nodes if n not in stale]
        self._force_rerun |= stale
        self._skipped_steps -= stale
        for name in sorted(stale):
            self._on_state_change(name, NodeState.PENDING)
        return sorted(stale)

    def run_node(self, node_name, graph=None, params_list=None, use_cache=False,
                 debug=False, show_kpl=True, show_kpm=False, recursive=True,
                 line_callback=None, trigger_jump_forward=True):
        """
        Run a single node.  Returns True on success, False on failure.

        trigger_jump_forward – after this node completes, also run whatever
        it jump-links to (side effect only). Left True for the CLI/GUI
        single-node run path (kpl run <node>) so a jump chain keeps going on
        its own; PipelineExecutor passes False since a full-graph run already
        includes jump successors via the normal dependency-ordered schedule,
        and running them here too would bypass its cpu/gpu worker limits.
        """
        if node_name in self._executed_nodes:
            return True
        # Already being resolved higher up this call stack (e.g. this is a
        # jump predecessor whose own jump-successor points back at the node
        # that just pulled it in) — the ancestor call owns running it.
        if node_name in self._in_progress_nodes:
            return True

        if not _require_config():
            return False

        if graph is None:
            graph = KGraph(verbose=False)
            graph.load_externals()
            graph.load_from_yaml(self.GRAPH_CONFIG)
            _warn_missing(graph)

        node = graph.get_node(node_name)
        if not node:
            print(f"Node '{node_name}' not found in graph.")
            return False

        self._in_progress_nodes.add(node_name)
        try:
            return self._run_node_inner(
                node_name, node, graph, params_list, use_cache, debug,
                show_kpl, show_kpm, recursive, line_callback, trigger_jump_forward,
            )
        finally:
            self._in_progress_nodes.discard(node_name)

    def _has_output_cache(self, node, graph):
        """Whether node has completed at least one run whose results are
        still on disk (uuid-scoped `outputs.yaml`). Same existence check
        `status()` uses for its `[cached]` marker."""
        uuid_val = graph.externals.get('uuid', '123456789')
        outputs_yaml = os.path.join(graph.get_cache_dir(), uuid_val, node.name, 'outputs.yaml')
        return os.path.exists(outputs_yaml)

    def _run_node_inner(self, node_name, node, graph, params_list, use_cache,
                         debug, show_kpl, show_kpm, recursive, line_callback,
                         trigger_jump_forward):
        # Optional jump_links: nodes this one must run after, with no data
        # exchanged. Only run a predecessor if it hasn't completed before —
        # unlike a real nodes.X.Y reference there's no value to fetch, so the
        # signal is just "does it have any cached result on disk" (gated by
        # use_cache exactly like the real-reference check below, so
        # --no-use-cache forces it to re-run too).
        for jump_src in graph.get_jump_predecessors(node_name):
            if not recursive:
                print(f'  Warning: jump-after {jump_src} but recursive=False.')
                continue
            if jump_src in self._skipped_steps:
                continue
            src_node = graph.get_node(jump_src)
            if (use_cache and jump_src not in self._force_rerun
                    and src_node and self._has_output_cache(src_node, graph)):
                if jump_src not in self._executed_nodes:
                    self._executed_nodes.append(jump_src)
                continue
            ok = self.run_node(
                jump_src, graph=graph,
                use_cache=use_cache, debug=debug,
                show_kpl=show_kpl, show_kpm=show_kpm,
                recursive=True, line_callback=line_callback,
            )
            if not ok:
                return False

        params = {}

        # No explicit params_list means this node was reached recursively (a
        # dependency or jump predecessor). During a `kpl group run` with
        # --set overrides, it may still have one waiting — see run_group().
        if params_list is None:
            params_list = getattr(self, '_pending_overrides', {}).get(node_name)

        if params_list:
            for i in range(0, len(params_list) - 1, 2):
                key = params_list[i].lstrip('-')
                params[key] = params_list[i + 1]

        uuid_val = graph.externals.get('uuid', '123456789')
        params['uuid'] = uuid_val
        params['node_id'] = node_name

        # Resolve input references. We still make sure the source node has
        # actually run (unchanged - this is what guarantees execution order
        # for kwui sequencing, GUI run-order, etc.). Whether we then also
        # read its cached value here (eager) or defer that read to the node
        # subprocess itself, at the point node.py code touches
        # self.inputs[x].value (lazy; see SourceNodeRef/KParameter in
        # knode.py/kparameter.py), is config.yaml's `lazy_input_resolution`
        # (default: lazy).
        lazy = _lazy_input_resolution_enabled()
        ref_sources = {}
        for inp_name, inp_param in node.inputs.items():
            if inp_name in params:
                continue
            if inp_param.is_reference():
                ref_node_id, ref_out_name = inp_param.resolve_reference()
                if not recursive:
                    print(f'  Warning: {inp_name} references {ref_node_id} but recursive=False.')
                    continue
                if ref_node_id:
                    ref_node = graph.get_node(ref_node_id)
                    if ref_node:
                        has_cache = None
                        if use_cache and ref_node_id not in self._force_rerun:
                            has_cache = ref_node.outputs[ref_out_name].get_cached_value(
                                uuid_val, ref_node_id, graph.get_cache_dir()
                            ) if ref_out_name in ref_node.outputs else None
                            if has_cache is not None and ref_node_id not in self._executed_nodes:
                                self._executed_nodes.append(ref_node_id)
                        if has_cache is None:
                            ok = self.run_node(
                                ref_node_id, graph=graph,
                                use_cache=use_cache, debug=debug,
                                show_kpl=show_kpl, show_kpm=show_kpm,
                                recursive=True, line_callback=line_callback,
                            )
                            if not ok:
                                return False
                            if not lazy and ref_out_name in ref_node.outputs:
                                has_cache = ref_node.outputs[ref_out_name].get_cached_value(
                                    uuid_val, ref_node_id, graph.get_cache_dir()
                                )
                        if lazy:
                            ref_sources[inp_name] = {'node': ref_node_id, 'output': ref_out_name}
                        else:
                            params[inp_name] = has_cache
            else:
                params[inp_name] = inp_param.value
        if ref_sources:
            params['input_sources'] = json.dumps(ref_sources)

        # node.groups (declared slots + this instance's bindings, see
        # KNode._load_from_yaml/KGraph.load_from_yaml) -> self.groups[name]
        # in node.py. Empty for every node type that doesn't declare any
        # (the default) - this whole block is then a no-op, same generic
        # passthrough as input_sources above (execute() turns any params
        # key into a --key=value CLI flag, no changes needed there).
        if getattr(node, 'groups', None):
            groups_payload = _resolve_underlying_groups(node.groups, self.GRAPH_CONFIG)
            if groups_payload:
                params['groups'] = json.dumps(groups_payload)

        # node.underlying_nodes: same passthrough for underlying-NODE slots
        # (see _resolve_underlying_nodes). A no-op for every node type that
        # declares none — the vast majority, and every pre-existing one.
        if getattr(node, 'underlying_nodes', None):
            nodes_payload = _resolve_underlying_nodes(node.underlying_nodes, graph)
            if nodes_payload:
                params['underlying_nodes'] = json.dumps(nodes_payload)

        # Execute
        print(f'\n---------- Running < {node_name} > ----------')
        node.state = NodeState.RUNNING
        self._on_state_change(node_name, NodeState.RUNNING)

        ret = node.execute(
            params=params,
            show_progress=True,
            show_kpl=show_kpl,
            show_kpm=show_kpm,
            debug=debug,
            line_callback=line_callback,
        )

        if ret != 0:
            node.state = NodeState.FAILED
            node.error = f'Exit code {ret}'
            self._on_state_change(node_name, NodeState.FAILED)
            print(f"\n[ERROR] Node '{node_name}' failed with exit code {ret}.")
            return False

        node.state = NodeState.COMPLETED
        self._on_state_change(node_name, NodeState.COMPLETED)
        self._executed_nodes.append(node_name)
        self._force_rerun.discard(node_name)
        self._skipped_steps.discard(node_name)
        print(f'[OK] {node_name}')

        # A node with no declared outputs never calls save_output(), so
        # outputs.yaml never gets written — which would make it look
        # perpetually un-cached to _has_output_cache() above (and to
        # status()'s own [cached] marker) even after finishing successfully.
        # That matters specifically for jump links: a plain ordering-only
        # node is expected to have no outputs at all. Touch an empty
        # outputs.yaml so "has this node completed once" is reliable either way.
        outputs_yaml = os.path.join(node.output_dir, 'outputs.yaml')
        if not os.path.exists(outputs_yaml):
            os.makedirs(node.output_dir, exist_ok=True)
            with open(outputs_yaml, 'w') as f:
                yaml.dump({}, f)

        if trigger_jump_forward:
            for jump_tgt in graph.get_jump_successors(node_name):
                ok = self.run_node(
                    jump_tgt, graph=graph,
                    use_cache=use_cache, debug=debug,
                    show_kpl=show_kpl, show_kpm=show_kpm,
                    recursive=True, line_callback=line_callback,
                    trigger_jump_forward=trigger_jump_forward,
                )
                if not ok:
                    return False

        return True

    def _write_run_state(self):
        """Mirror the current node states to cache/run_state.json (atomic).

        Read by the GUI server of the same workdir to passively highlight
        externally running nodes. The pid lets readers detect stale files
        (writer no longer alive) and `source` distinguishes terminal runs
        from GUI-spawned ones (which the GUI already tracks via stdout).

        A `kpl` started BY a node of a kwui run (GroupRef/NodeRef.run, a node that
        calls kpl itself) does not write it: that file belongs to the kwui run, whose
        pid is the one that stays alive. A nested writer would replace it with its own
        pid and states, and once it ended the GUI would see a dead writer and stop
        highlighting the step that is still running. Instead it records its own entry
        in cache/run_state_nested.json (see _write_nested_run_state), so the GUI can
        highlight the kwui step AND the nodes the nested run is executing. kpl marks
        the node processes of a kwui run with this variable (see KNode.execute /
        kwui.RUN_TOKEN_ENV); it is absent in every other run, which is therefore
        unchanged."""
        if os.environ.get('KPL_KWUI_RUN_TOKEN'):
            self._write_nested_run_state()
            return
        cache_dir, _ = read_configured_dirs(os.getcwd())
        path = os.path.join(cache_dir, 'run_state.json')
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f'{path}.{os.getpid()}.tmp'
            with open(tmp, 'w') as f:
                json.dump({
                    'pid': os.getpid(),
                    'source': self._run_source,
                    'updated': time.time(),
                    'states': self._node_states,
                }, f)
            os.replace(tmp, path)
        except Exception:
            pass  # state mirroring must never break a run

    def _write_nested_run_state(self):
        """A nested kpl's own entry (pid -> node states) in cache/run_state_nested.json.

        Several processes may write this file, so every update is a read-modify-write
        under an exclusive lock; each process only ever touches ITS OWN entry (and drops
        entries of processes that no longer exist), which is why — unlike sharing the
        main run_state.json — nothing can be lost or left standing by a race. The GUI
        reads the entries of live processes only, so a nested run that was killed
        (Reset) leaves nothing highlighted."""
        cache_dir, _ = read_configured_dirs(os.getcwd())
        path = os.path.join(cache_dir, 'run_state_nested.json')
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path + '.lock', 'a') as lock:
                try:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX)
                except ImportError:
                    pass  # no flock on this platform: write unlocked
                try:
                    with open(path) as f:
                        procs = (json.load(f) or {}).get('procs') or {}
                except (OSError, ValueError):
                    procs = {}
                procs = {pid: entry for pid, entry in procs.items() if _pid_alive(pid)}
                procs[str(os.getpid())] = {'updated': time.time(),
                                           'states': dict(self._node_states)}
                tmp = f'{path}.{os.getpid()}.tmp'
                with open(tmp, 'w') as f:
                    json.dump({'procs': procs}, f)
                os.replace(tmp, path)
        except Exception:
            pass  # state mirroring must never break a run

    def _on_state_change(self, name, state, error=None):
        with self._state_lock:
            self._node_states[name] = state if isinstance(state, str) else str(state)
            self._write_run_state()
        icons = {
            NodeState.PENDING: '○',
            NodeState.WAITING: '◎',
            NodeState.RUNNING: '►',
            NodeState.COMPLETED: '✓',
            NodeState.FAILED: '✗',
            NodeState.SKIPPED: '⊘',
        }
        colors = {
            NodeState.RUNNING: '\033[93m',
            NodeState.COMPLETED: '\033[92m',
            NodeState.FAILED: '\033[91m',
            NodeState.SKIPPED: '\033[90m',
        }
        reset = '\033[0m'
        icon = icons.get(state, '?')
        color = colors.get(state, '')
        msg = f'  {color}{icon} {name}: {state}{reset}'
        if error:
            msg += f'  → {error}'
        print(msg, flush=True)
        state_str = state if isinstance(state, str) else str(state)
        print(f'<KPS> {json.dumps({"node": name, "state": state_str})} </KPS>', flush=True)
        # Single choke point both the sequential and parallel run paths already
        # go through — no-op unless a kwui workflow UI is live.
        from kpipeline.src.kwui import notify_state
        notify_state(name, state_str)

    # -----------------------------------------------------------------------
    # Tree
    # -----------------------------------------------------------------------

    def tree(self):
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_from_yaml(self.GRAPH_CONFIG)

        print(f'\nGraph: {graph.name}')
        print(f'Nodes: {len(graph.nodes)}\n')

        for node in graph.nodes:
            jump_deps = graph.get_jump_predecessors(node.name)
            deps = [d for d in graph.get_dependencies(node.name) if d not in jump_deps]
            dep_str = ' ← ' + ', '.join(deps) if deps else ''
            jump_str = ' ⇢ jump-after ' + ', '.join(jump_deps) if jump_deps else ''
            print(f'  [{node.type}] {node.name}{dep_str}{jump_str}')
            for k, v in node.inputs.items():
                print(f'      in  {k} = {v.value}')
            for k in node.outputs:
                print(f'      out {k}')
        print()
        return True

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def status(self):
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_externals()
        graph.load_from_yaml(self.GRAPH_CONFIG)

        uuid_val = graph.externals.get('uuid', '123456789')
        print(f'\nGraph: {graph.name}  (uuid={uuid_val})\n')

        for node in graph.nodes:
            cache_dir = os.path.join(graph.get_cache_dir(), uuid_val, node.name)
            has_cache = os.path.exists(os.path.join(cache_dir, 'outputs.yaml'))
            cached = '[cached]' if has_cache else ''
            frozen = '[frozen]' if node.name in graph.frozen_nodes else ''
            print(f'  {node.name} ({node.type}) {cached} {frozen}')
        print()

    # -----------------------------------------------------------------------
    # Clean cache
    # -----------------------------------------------------------------------

    def clean_cache(self, node_id=None):
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_externals()
        graph.load_from_yaml(self.GRAPH_CONFIG)

        uuid_val = graph.externals.get('uuid', '123456789')
        cache_root = os.path.join(graph.get_cache_dir(), uuid_val)

        if node_id:
            target = os.path.join(cache_root, node_id)
            if os.path.exists(target):
                shutil.rmtree(target)
                print(f"Cleaned cache for '{node_id}'.")
            else:
                print(f"No cache for '{node_id}'.")
        else:
            for node in graph.nodes:
                if node.name in graph.frozen_nodes:
                    print(f"  Skipping frozen node '{node.name}'.")
                    continue
                target = os.path.join(cache_root, node.name)
                if os.path.exists(target):
                    shutil.rmtree(target)
                    print(f"  Cleaned '{node.name}'.")
                else:
                    print(f"  No cache for '{node.name}'.")

    # -----------------------------------------------------------------------
    # Groups
    # -----------------------------------------------------------------------

    def list_groups(self):
        if not _require_config():
            return False
        groups, group_links, member_map = _load_group_info(self.GRAPH_CONFIG)
        if not groups:
            print('No groups in this pipeline.')
            return True
        for g in groups:
            gid = g['id']
            label = g.get('label', gid)
            members = member_map.get(gid, [])
            outs = _group_output_nodes(gid, group_links)
            print(f"{label}  (id={gid})")
            print(f"    members:      {', '.join(members) or '-'}")
            print(f"    outputs from: {', '.join(outs) or '-'}")
        return True

    def clean_group_cache(self, group_ref):
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_from_yaml(self.GRAPH_CONFIG)
        groups, _group_links, member_map = _load_group_info(self.GRAPH_CONFIG)
        grp = _resolve_group(groups, group_ref)
        if not grp:
            print(f"No group matching '{group_ref}'. Run `kpl group list` to see available groups.")
            return False
        label = grp.get('label', grp['id'])
        members = member_map.get(grp['id'], [])
        if not members:
            print(f"Group '{label}' has no member nodes.")
            return True
        print(f"Cleaning cache for group '{label}' ({len(members)} node(s))...")
        # Delete each member's cache dir directly (same computation
        # clean_cache(node_id=...) does) instead of calling clean_cache() in
        # this loop — that would rebuild the WHOLE KGraph (a base-nodes
        # directory scan + parsing every node's node_config.yaml) once per
        # member, N+1 times total for an N-member group. self.groups[x].
        # remove_cache() (knode.py) shells out to this command, so that
        # redundant cost was paid inside every running node that called it.
        uuid_val = graph.externals.get('uuid', '123456789')
        cache_root = os.path.join(graph.get_cache_dir(), uuid_val)
        for node_name in members:
            # frozen_nodes is respected here even though a single `kpl clean
            # <node>` isn't (that CLI path targets one node explicitly, by
            # design) — a group-wide clean is closer to "clean everything"
            # and should honor the same "frozen survives" contract.
            if node_name in graph.frozen_nodes:
                print(f"  Skipping frozen node '{node_name}'.")
                continue
            target = os.path.join(cache_root, node_name)
            if os.path.exists(target):
                shutil.rmtree(target)
                print(f"Cleaned cache for '{node_name}'.")
            else:
                print(f"No cache for '{node_name}'.")
        return True

    def run_group(self, group_ref, use_cache=True, debug=False, overrides=None):
        """Run whatever feeds the group's declared outputs (+ their own
        dependencies, resolved as usual) — sequential only, same as `kpl run
        <node>`; not wired to the parallel PipelineExecutor.

        overrides: optional {declared_group_input_name: value} — a one-off
        override for this run only (never written to graph_config.yaml),
        resolved via the group's own input-port mapping to whichever inner
        node/port it actually feeds. Used by `kpl group run --set k=v` and by
        GroupRef.run() (knode.py) — self.groups[...].inputs[...] = value."""
        if not _require_config():
            return False
        graph = KGraph(verbose=False)
        graph.load_externals()
        graph.load_from_yaml(self.GRAPH_CONFIG)
        _warn_missing(graph)
        groups, group_links, _member_map = _load_group_info(self.GRAPH_CONFIG)
        grp = _resolve_group(groups, group_ref)
        if not grp:
            print(f"No group matching '{group_ref}'. Run `kpl group list` to see available groups.")
            return False
        label = grp.get('label', grp['id'])
        targets = _group_output_nodes(grp['id'], group_links)
        if not targets:
            print(f"Group '{label}' has no outputs wired up — nothing to run.")
            return False

        # Resolve overrides to per-node params_list segments. Kept separate
        # from `targets` (an output-producing node can ALSO be the direct
        # consumer of an overridden input — a "single-node group" is a
        # common, valid shape) so each affected node gets exactly ONE
        # run_node() call carrying its override, rather than one call to
        # apply the override followed by a second, un-overridden run when
        # `targets` runs it again — `run_node` always re-executes a node
        # it's given as its own direct target (only a node's OWN
        # dependencies are cache-skipped), so a second call would silently
        # stomp the first with a fresh, un-overridden result.
        node_params = {}
        if overrides:
            input_map = _group_input_map(grp['id'], group_links)
            for name, value in overrides.items():
                for node_id, port_name in input_map.get(name, []):
                    node_params.setdefault(node_id, []).extend([f'--{port_name}', str(value)])

        # Only what feeds the group's outputs runs (plus their upstream
        # dependencies and jump predecessors). Jump links pointing AWAY from
        # these nodes are NOT followed (trigger_jump_forward=False below):
        # whatever merely comes after them has no data connection to the
        # group and must not run as a side effect.
        print(f"\nRunning group '{label}' -> {', '.join(targets)}\n")
        # Publish the overrides for the whole group run, not just for the
        # explicit run_node() calls below. An overridden node can be reached
        # TRANSITIVELY first — as a dependency or jump predecessor of an
        # EARLIER overridden node in this same loop — and that recursive
        # path carries no params_list. It would then run un-overridden, land
        # in _executed_nodes, and its own explicit call below would hit
        # run_node()'s "already executed" guard and return True without ever
        # applying the override, silently dropping it. _run_node_inner falls
        # back to this map whenever it's called without an explicit
        # params_list, so whichever path reaches a node first still applies
        # its override. (Real bug: laser_check_group staged table_center +
        # red/green_laser_on; table_center's consumer pulled in laser_ctrl_2
        # through the jump chain first, so the laser flags never took effect
        # and the lasers stayed off.)
        self._pending_overrides = dict(node_params)
        try:
            # Any overridden node that ISN'T itself one of the group's direct
            # output targets is an upstream dependency — apply its override now,
            # so the targets below see the fresh value through the normal
            # cache/dependency mechanism when they run.
            for node_id, params_list in node_params.items():
                if node_id not in targets:
                    ok = self.run_node(node_id, graph=graph, params_list=params_list,
                                        use_cache=use_cache, debug=debug,
                                        show_kpl=debug, show_kpm=not debug,
                                        trigger_jump_forward=False)
                    if not ok:
                        print(f"\n[FAILED] Group '{label}' stopped at node '{node_id}'.")
                        return False
            for name in targets:
                ok = self.run_node(name, graph=graph, params_list=node_params.get(name),
                                    use_cache=use_cache, debug=debug,
                                    show_kpl=debug, show_kpm=not debug,
                                    trigger_jump_forward=False)
                if not ok:
                    print(f"\n[FAILED] Group '{label}' stopped at node '{name}'.")
                    return False
            return True
        finally:
            self._pending_overrides = {}

    # -----------------------------------------------------------------------
    # Node env
    # -----------------------------------------------------------------------

    def install_node_env(self, node_type=None, category=None):
        # Resolve through the pipeline's effective dirs when a graph config
        # is present (so envs of custom-dir nodes install too); otherwise
        # fall back to the installation default dir.
        dirs = None
        graph = None
        if os.path.exists(self.GRAPH_CONFIG):
            graph = KGraph(verbose=False)
            graph.load_from_yaml(self.GRAPH_CONFIG)
            dirs = graph.get_base_nodes_dirs()

        if node_type:
            types = [node_type]
        elif category:
            # Every node type living in this category's directory (same
            # grouping `kpl list` shows under a "[category]" heading) - lets
            # a whole category's node types be installed in one call, e.g.
            # when several of them share one env_directory (a common venv
            # for the category) instead of each getting its own.
            if not _require_config() or graph is None:
                return False
            types = [
                nt for nt, info in scan_base_nodes(dirs).items()
                if info['category'] == category
            ]
            if not types:
                print(f"No node types found in category '{category}'.")
                return False
        else:
            if not _require_config() or graph is None:
                return False
            types = list({n.type for n in graph.nodes})

        for nt in types:
            nt_dir = find_node_dir(nt, PIPELINER_DIR, dirs=dirs)
            if not nt_dir:
                print(f"  Type '{nt}' not found.")
                continue
            cfg_path = os.path.join(nt_dir, 'node_config.yaml')
            node = KNode.__new__(KNode)
            node.name = nt
            node.type = nt
            node.inputs = {}
            node.outputs = {}
            node._verbose = False
            node.node_dir = nt_dir
            node.env_manager = None
            node.env_directory = '.venv'
            node.requirements = None
            node._load_from_yaml(cfg_path)
            print(f"Installing env for '{nt}'...")
            node.install_env()


# ---------------------------------------------------------------------------
# Base node management (module-level, matches reference API)
# ---------------------------------------------------------------------------

def create_base_node():
    for src, dst in [
        ('config.node_template.yaml', 'node_config.yaml'),
        ('node_template.py', 'node.py'),
    ]:
        template = os.path.join(PIPELINER_DIR, 'kpipeline', 'templates', src)
        if os.path.exists(dst):
            print(f"'{dst}' already exists – skipping.")
        else:
            shutil.copy2(template, dst)
            print(f"Created '{dst}'.")


def install_base_node(node_name=None):
    # base_node install/uninstall manage the installation's DEFAULT dir only;
    # pipeline-specific custom_base_nodes_dirs are managed by hand.
    nodes_dir = get_default_base_nodes_dir(PIPELINER_DIR)
    os.makedirs(nodes_dir, exist_ok=True)
    current_dir = os.getcwd()
    name = node_name or os.path.basename(current_dir)
    existing = find_node_dir(name, PIPELINER_DIR)
    dest = existing or os.path.join(nodes_dir, name)

    if os.path.exists(dest):
        print(f"Node '{name}' already exists. Overwrite? (y/n): ", end='')
        if input().strip().lower() != 'y':
            print('Aborted.')
            return False
        shutil.rmtree(dest)

    shutil.copytree(current_dir, dest,
                    ignore=shutil.ignore_patterns('.venv', '__pycache__', '*.pyc', '.git'))
    print(f"Installed '{name}' → {dest}")
    return True


def uninstall_base_node(node_name):
    dest = find_node_dir(node_name, PIPELINER_DIR)
    if not dest:
        print(f"Node '{node_name}' not found.")
        return False
    print(f"Remove '{dest}'? (y/n): ", end='')
    if input().strip().lower() != 'y':
        print('Aborted.')
        return False
    shutil.rmtree(dest)
    print(f"Uninstalled '{node_name}'.")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_config():
    if not os.path.exists('graph_config.yaml'):
        print("No 'graph_config.yaml' found. Run: kpl init")
        return False
    return True


def _kwui_server_defaults():
    """Where `kpl run --kwui` serves workflow.py: config.yaml's `kwui:` section
    (host / port / open_browser), falling back to kwui.py's defaults. Overridden
    per run by --kwui-host / --kwui-port / --kwui-no-browser."""
    from kpipeline.src.kwui import DEFAULT_HOST, DEFAULT_PORT
    defaults = {'host': DEFAULT_HOST, 'port': DEFAULT_PORT, 'open_browser': True}
    cfg_path = os.path.join(PIPELINER_DIR, 'config.yaml')
    if not os.path.exists(cfg_path):
        return defaults
    try:
        with open(cfg_path) as f:
            section = (yaml.safe_load(f) or {}).get('kwui') or {}
    except Exception:
        return defaults
    if section.get('host'):
        defaults['host'] = str(section['host'])
    if section.get('port'):
        defaults['port'] = int(section['port'])
    if 'open_browser' in section:
        defaults['open_browser'] = bool(section['open_browser'])
    return defaults


def _lazy_input_resolution_enabled():
    """Whether a `nodes.X.Y` input is resolved lazily (child subprocess reads
    the cached value itself on first access) or eagerly (parent reads it up
    front and bakes it into the subprocess's CLI args) — config.yaml's
    `lazy_input_resolution` key, defaulting to True (lazy)."""
    cfg_path = os.path.join(PIPELINER_DIR, 'config.yaml')
    if not os.path.exists(cfg_path):
        return True
    try:
        with open(cfg_path) as f:
            section = yaml.safe_load(f) or {}
    except Exception:
        return True
    if 'lazy_input_resolution' in section:
        return bool(section['lazy_input_resolution'])
    return True


def _mcp_package_available():
    """Whether the optional `mcp` package (kpl mcp / kpl gui --mcp only —
    not needed to build or run pipelines) is installed in this interpreter."""
    return importlib.util.find_spec('mcp') is not None


def _print_mcp_missing():
    print("MCP support needs the optional 'mcp' package (not installed).")
    print(f'Install it with: {sys.executable} -m pip install -r '
          f"{os.path.join(PIPELINER_DIR, 'requirements-mcp.txt')}")


def _warn_missing(graph):
    if graph.missing_nodes:
        names = ', '.join(f"{m['name']} ({m['type']})" for m in graph.missing_nodes)
        print(f'Warning: nodes missing from base nodes were skipped: {names}')


def _load_graph_config(graph):
    if os.path.exists('graph_config.yaml'):
        try:
            graph.load_from_yaml('graph_config.yaml')
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Groups (GUI-only concept — KGraph/PipelineExecutor have no notion of a
# group, see CLAUDE.md "Executor is group-agnostic"; these helpers read
# graph_config.yaml's `groups`/`group_links`/node.gui.group directly, same as
# kpipeline_gui/server.py does for the GUI, rather than teaching KGraph
# about them).
# ---------------------------------------------------------------------------

def _load_group_info(graph_config_path):
    """Returns (groups, group_links, member_map) — member_map: {group_id:
    [node_name, ...]} from each node's gui.group tag."""
    with open(graph_config_path) as f:
        cfg = yaml.safe_load(f) or {}
    groups = cfg.get('groups', []) or []
    group_links = cfg.get('group_links', []) or []
    member_map = {}
    for node_cfg in cfg.get('nodes', []) or []:
        gid = (node_cfg.get('gui') or {}).get('group')
        if gid:
            member_map.setdefault(gid, []).append(node_cfg['name'])
    return groups, group_links, member_map


def _resolve_group(groups, ref):
    """Match `ref` against a group id first, then its (optional, non-unique)
    label — case-insensitive, only if exactly one group has that label."""
    for g in groups:
        if g.get('id') == ref:
            return g
    matches = [g for g in groups
               if (g.get('label') or '').strip().lower() == ref.strip().lower()]
    return matches[0] if len(matches) == 1 else None


def _group_output_nodes(group_id, group_links):
    """Inner node names that feed one of the group's declared outputs — i.e.
    group_links entries routed INTO this group's gout-<name> port from
    inside (source=inner node, target=group, targetHandle='gout-<name>').
    Running these (+ their own dependencies, resolved as usual) is what
    'running the group' means, since a group's outputs are the only thing
    anything outside it can depend on."""
    return sorted({
        l['source'] for l in group_links
        if l.get('target') == group_id and str(l.get('targetHandle', '')).startswith('gout-')
    })


def _group_output_map(group_id, group_links):
    """{declared_output_name: [inner_node_id, inner_output_name]} - same
    group_links entries as _group_output_nodes above, but keeping the
    per-port mapping instead of collapsing to just node names (that's all
    `kpl group run` ever needed; self.groups[...].outputs in knode.py needs
    to know exactly which port on which node backs a given declared name)."""
    result = {}
    for l in group_links:
        if l.get('target') == group_id and str(l.get('targetHandle', '')).startswith('gout-'):
            name = l['targetHandle'][len('gout-'):]
            result[name] = [l['source'], l.get('sourceHandle')]
    return result


def _group_input_map(group_id, group_links):
    """{declared_input_name: [[inner_node_id, inner_input_name], ...]} - a
    group input can fan out to several inner consumers (hence a list, unlike
    _group_output_map). Mirrors it for the input side: the SECOND hop of
    'outside -> gin -> ... -> inside.in' (source=group, sourceHandle=
    'gin-<name>', target=inner node, targetHandle=inner input name)."""
    result = {}
    for l in group_links:
        if l.get('source') == group_id and str(l.get('sourceHandle', '')).startswith('gin-'):
            name = l['sourceHandle'][len('gin-'):]
            result.setdefault(name, []).append([l['target'], l.get('targetHandle')])
    return result


def _resolve_underlying_groups(node_groups, graph_config_path):
    """{slot_name: {'group_id': id-or-None, 'inputs': {...}, 'outputs': {...}}}
    for a node's self.groups (name -> KParameter whose .value is the bound
    group's id, or None if this slot isn't bound to anything yet - see this
    node's Properties panel). One entry per DECLARED slot, bound or not, so
    the child can build a self.groups[name] for every one of them and raise
    a clear "not bound" error at use-time (GroupRef.run()/.inputs/.outputs)
    rather than a bare KeyError at self.groups[name] lookup time. A bound id
    that no longer matches any group in this pipeline (e.g. the group was
    since deleted) is treated the same as unbound."""
    if not node_groups:
        return {}
    groups, group_links, _member_map = _load_group_info(graph_config_path)
    by_id = {g['id']: g for g in groups}
    result = {}
    for name, kparam in node_groups.items():
        grp = by_id.get(kparam.value) if kparam.value else None
        if grp:
            result[name] = {
                'group_id': grp['id'],
                'inputs': _group_input_map(grp['id'], group_links),
                'outputs': _group_output_map(grp['id'], group_links),
            }
        else:
            result[name] = {'group_id': None, 'inputs': {}, 'outputs': {}}
    return result


def _resolve_underlying_nodes(node_slots, graph):
    """{slot_name: {'node_id': id-or-None, 'type': ..., 'inputs': [names],
    'outputs': [names]}} for a node's underlying_nodes (name -> KParameter
    whose .value is the bound node's name, or None if this slot isn't bound
    yet). Same contract as _resolve_underlying_groups above: one entry per
    DECLARED slot, bound or not, so the child can raise a clear "not bound"
    error at use-time; a bound name that no longer matches any node in this
    pipeline (deleted/renamed since) is treated as unbound. The bound node is
    only DESCRIBED here — it is not run, and it is not a dependency."""
    if not node_slots:
        return {}
    result = {}
    for name, kparam in node_slots.items():
        target = graph.get_node(kparam.value) if kparam.value else None
        if target:
            result[name] = {
                'node_id': target.name,
                'type': target.type,
                'inputs': list(target.inputs),
                'outputs': list(target.outputs),
            }
        else:
            result[name] = {'node_id': None, 'type': None, 'inputs': [], 'outputs': []}
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _add_kwui_server_args(parser):
    """--kwui's server options, shared by `run` and `run_debug`. Defaults stay
    None so config.yaml's `kwui:` section decides (see _kwui_server_defaults)."""
    parser.add_argument('--kwui-host', default=None,
                        help='Interface the kwui UI listens on (default: 0.0.0.0, '
                             'i.e. reachable from other machines; 127.0.0.1 for '
                             'this machine only)')
    parser.add_argument('--kwui-port', type=int, default=None,
                        help='Port for the kwui UI (default 8701; the first free '
                             'port at or above it is used)')
    parser.add_argument('--kwui-no-browser', action='store_true',
                        help='Do not open a browser for the kwui UI')


def _kwui_server_args(args):
    """The parsed --kwui-* options as run_kwui() kwargs. `open_browser` stays
    None unless --kwui-no-browser was passed, so config.yaml keeps deciding."""
    return {
        'host': getattr(args, 'kwui_host', None),
        'port': getattr(args, 'kwui_port', None),
        'open_browser': False if getattr(args, 'kwui_no_browser', False) else None,
    }


def main():
    parser = argparse.ArgumentParser(
        prog='kpl',
        description='kpl (kpipeliner) – node-based pipeline manager',
    )
    sub = parser.add_subparsers(dest='command', required=False)

    # init
    sub.add_parser('init', help='Initialize pipeline in current directory')

    # gui
    p_gui = sub.add_parser('gui', help='Launch the web GUI')
    p_gui.add_argument('--mcp', action='store_true',
                        help='Also mount an MCP endpoint at /mcp for LLM clients')

    # mcp (headless, no GUI/REST API at all)
    sub.add_parser('mcp', help='Run a headless MCP server for LLM clients (no GUI)')

    # list
    sub.add_parser('list', help='List available node types')

    # add
    p_add = sub.add_parser('add', help='Add a node to the graph')
    p_add.add_argument('node_type')
    p_add.add_argument('node_id', nargs='?')

    # run
    p_run = sub.add_parser('run', help='Run graph or a specific node')
    p_run.add_argument('node_id', nargs='?')
    p_run.add_argument('params', nargs=argparse.REMAINDER, default=None)
    p_run.add_argument('--no-parallel', action='store_true')
    p_run.add_argument('--no-use-cache', dest='use_cache', action='store_false',
                       help='Recompute instead of reusing cached node results')
    p_run.add_argument('--exit-status', action='store_true',
                       help='Exit with status 1 if the run failed (default: always 0, as '
                            'for every other kpl command). Used by NodeRef.run(). With '
                            '<node_id>, must come BEFORE it.')
    p_run.add_argument('--no-jump-forward', action='store_true',
                       help='With <node_id>: do not run the node\'s jump-link successors '
                            'afterwards (used by NodeRef.run() — a node run as a subroutine). '
                            'Must come BEFORE <node_id>.')
    p_run.add_argument('--kwui', action='store_true',
                       help='Run through workflow.py\'s kwebui UI (see: kpl kwui init)')
    _add_kwui_server_args(p_run)

    # run_debug
    p_dbg = sub.add_parser('run_debug', help='Run in debug mode (full log output)')
    p_dbg.add_argument('node_id', nargs='?')
    p_dbg.add_argument('params', nargs=argparse.REMAINDER, default=None)
    p_dbg.add_argument('--no-parallel', action='store_true')
    p_dbg.add_argument('--no-use-cache', dest='use_cache', action='store_false',
                       help='Recompute instead of reusing cached node results')
    p_dbg.add_argument('--kwui', action='store_true',
                       help='Run through workflow.py\'s kwebui UI (see: kpl kwui init)')
    _add_kwui_server_args(p_dbg)

    # kwui
    p_kwui = sub.add_parser('kwui', help='Manage the kwebui workflow UI')
    kwui_sub = p_kwui.add_subparsers(dest='kwui_command', required=True)
    kwui_sub.add_parser('init', help='Create workflow.py in this pipeline directory')

    # tree
    sub.add_parser('tree', help='Print graph tree')

    # status
    sub.add_parser('status', help='Show node cache status')

    # clean
    p_clean = sub.add_parser('clean', help='Clean cache')
    p_clean.add_argument('node_id', nargs='?')
    p_clean.add_argument('--exit-status', action='store_true',
                         help='Exit with status 1 if cleaning failed (default: always 0). '
                              'Used by NodeRef.remove_cache().')

    # group
    p_grp = sub.add_parser('group', help='Operate on a GUI group as a unit')
    grp_sub = p_grp.add_subparsers(dest='group_command', required=True)
    grp_sub.add_parser('list', help='List groups (id, label, members, outputs)')
    p_grp_clean = grp_sub.add_parser('clean', help="Clean cache for every node in a group")
    p_grp_clean.add_argument('group_id', help='Group id (or its label, if unambiguous)')
    p_grp_run = grp_sub.add_parser(
        'run', help="Run whatever feeds the group's declared outputs")
    p_grp_run.add_argument('group_id', help='Group id (or its label, if unambiguous)')
    p_grp_run.add_argument('--no-use-cache', dest='use_cache', action='store_false',
                            help='Recompute instead of reusing cached node results')
    p_grp_run.add_argument('--set', action='append', metavar='GROUP_INPUT=VALUE',
                            help="Override one of the group's declared inputs for this run "
                                 '(repeatable) - resolved to whichever inner node/port it '
                                 'feeds, same as a one-off `kpl run <node> --key value` '
                                 'override; never written to graph_config.yaml.')

    # base_node
    p_bn = sub.add_parser('base_node', help='Manage base node types')
    bn_sub = p_bn.add_subparsers(dest='node_command', required=True)
    bn_sub.add_parser('create', help='Create node template in current directory')
    p_install = bn_sub.add_parser('install', help='Install node from current directory')
    p_install.add_argument('--node_name', default=None)
    p_uninst = bn_sub.add_parser('uninstall', help='Uninstall a node type')
    p_uninst.add_argument('node_name')

    # node_env
    p_env = sub.add_parser('node_env', help='Manage node Python environments')
    env_sub = p_env.add_subparsers(dest='env_command', required=True)
    p_env_install = env_sub.add_parser('install', help='Install environment for node(s)')
    p_env_install.add_argument('--node_type', default=None)
    p_env_install.add_argument('--category', default=None,
                                help='Install every node type in this category (as shown by `kpl list`)')

    # uninstall kpipeliner itself
    sub.add_parser('uninstall', help='Uninstall kpl from PATH')

    args = parser.parse_args()
    p = Pipeliner()

    if args.command is None:
        parser.print_help()

    elif args.command == 'init':
        p.init_graph_dir()

    elif args.command == 'gui':
        p.run_gui(mcp=getattr(args, 'mcp', False))

    elif args.command == 'mcp':
        p.run_mcp()

    elif args.command == 'list':
        p.list_nodes()

    elif args.command == 'add':
        p.add_node(args.node_type, args.node_id)

    elif args.command == 'run':
        parallel = not getattr(args, 'no_parallel', False)
        use_cache = getattr(args, 'use_cache', True)
        if getattr(args, 'kwui', False):
            p.run_kwui(use_cache=use_cache, debug=False, **_kwui_server_args(args))
        elif args.node_id:
            ok = p.run_node(args.node_id, params_list=args.params,
                            use_cache=use_cache, show_kpl=False, show_kpm=False,
                            trigger_jump_forward=not getattr(args, 'no_jump_forward', False))
            # Opt-in only: every other kpl command exits 0 regardless of the
            # outcome (a project-wide gap, deliberately not changed broadly —
            # the GUI server and scripts may rely on it); NodeRef.run()
            # (knode.py) needs the real outcome, like GroupRef.run() does.
            if getattr(args, 'exit_status', False):
                sys.exit(0 if ok else 1)
        else:
            p.run_graph(parallel=parallel, use_cache=use_cache, debug=False)

    elif args.command == 'run_debug':
        parallel = not getattr(args, 'no_parallel', False)
        use_cache = getattr(args, 'use_cache', True)
        if getattr(args, 'kwui', False):
            p.run_kwui(use_cache=use_cache, debug=True, **_kwui_server_args(args))
        elif args.node_id:
            p.run_node(args.node_id, params_list=args.params,
                       use_cache=use_cache, debug=True, show_kpl=True, show_kpm=True)
        else:
            p.run_graph(parallel=parallel, use_cache=use_cache, debug=True)

    elif args.command == 'kwui':
        if args.kwui_command == 'init':
            p.kwui_init()

    elif args.command == 'tree':
        p.tree()

    elif args.command == 'status':
        p.status()

    elif args.command == 'clean':
        ok = p.clean_cache(node_id=args.node_id)
        # clean_cache() returns False only when it could not run at all (no
        # config); otherwise it falls through (None) — only False is failure.
        if getattr(args, 'exit_status', False):
            sys.exit(1 if ok is False else 0)

    elif args.command == 'group':
        if args.group_command == 'list':
            p.list_groups()
        elif args.group_command == 'clean':
            ok = p.clean_group_cache(args.group_id)
            # See the `group run` comment below — `group clean`'s exit code
            # is fixed the same way, narrowly, so GroupRef.remove_cache()
            # (knode.py) can tell whether it actually succeeded.
            sys.exit(0 if ok else 1)
        elif args.group_command == 'run':
            use_cache = getattr(args, 'use_cache', True)
            overrides = dict(kv.split('=', 1) for kv in (args.set or []))
            ok = p.run_group(args.group_id, use_cache=use_cache, debug=False, overrides=overrides)
            # Every OTHER kpl command's process exit code is 0 regardless of
            # success/failure (a pre-existing, project-wide gap out of scope
            # to fix broadly here) - but `group run` is new this session and
            # GroupRef.run() (knode.py) needs a real exit code to know
            # whether the group actually succeeded, so it gets this fixed
            # narrowly rather than inheriting the wider gap.
            sys.exit(0 if ok else 1)

    elif args.command == 'base_node':
        if args.node_command == 'create':
            create_base_node()
        elif args.node_command == 'install':
            install_base_node(args.node_name)
        elif args.node_command == 'uninstall':
            uninstall_base_node(args.node_name)

    elif args.command == 'node_env':
        if args.env_command == 'install':
            p.install_node_env(node_type=args.node_type, category=args.category)

    elif args.command == 'uninstall':
        _run_uninstall()

    else:
        parser.print_help()


def _run_uninstall():
    uninstall_sh = os.path.join(PIPELINER_DIR, 'install.sh')
    if not os.path.exists(uninstall_sh):
        print('install.sh not found.')
        return
    print('Running uninstaller...')
    subprocess.run(['bash', uninstall_sh, '--uninstall'])


if __name__ == '__main__':
    main()
