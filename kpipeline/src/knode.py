import yaml
import os
import sys
import json
import shlex
import subprocess
import inspect
import argparse
import logging
import traceback
from datetime import datetime

from kpipeline.src.kparameter import KParameter


class NodeState:
    PENDING = 'PENDING'
    WAITING = 'WAITING'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    SKIPPED = 'SKIPPED'


# self.log()'s levels, mapped onto Python's logging module. 'output' is a
# custom level (RESULT relays from execute()) sitting between INFO and
# WARNING; everything except the new 'debug' is >= INFO so a pipeline with no
# `log_level:` in graph_config.yaml filters nothing new — that's what keeps
# the default backward-compatible, not a special case.
OUTPUT_LEVEL = 25
logging.addLevelName(OUTPUT_LEVEL, 'OUTPUT')

_LEVEL_NAME_TO_NUM = {
    'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'OUTPUT': OUTPUT_LEVEL,
    'WARNING': logging.WARNING, 'ERROR': logging.ERROR,
}
_KPL_LEVEL_TO_NUM = {
    'debug': logging.DEBUG, 'info': logging.INFO, 'output': OUTPUT_LEVEL,
    'warning': logging.WARNING, 'error': logging.ERROR,
}
_KPL_COLORS = {
    'debug': '\033[90m', 'info': '\033[92m', 'warning': '\033[93m',
    'error': '\033[91m', 'output': '\033[96m',
}


class _KPLTerminalHandler(logging.Handler):
    """Renders self.log()'s legacy `<KPL>[level] <name> message` line, to a
    SAVED reference of the pristine stdout (self._node._real_stdout) — never
    to sys.stdout, which _PrintCapture below may have replaced by the time
    this runs. Splits multi-line messages (e.g. a traceback) into one <KPL>
    line per physical line: the parent's execute() reads one line at a time,
    so embedded raw newlines would silently drop everything past the first
    physical line instead of showing/logging the whole message."""

    def __init__(self, node):
        super().__init__()
        self._node = node

    def emit(self, record):
        kpl_level = getattr(record, 'kpl_level', record.levelname.lower())
        if kpl_level == 'status':
            # send_status_message() records: only meant for the log FILE
            # (the other handler on this logger) — the terminal/GUI already
            # gets the live <KPM> progress-bar line written straight to
            # _real_stdout; echoing it again here would just be noise.
            return
        color = _KPL_COLORS.get(kpl_level, '')
        reset = '\033[0m'
        stream = self._node._real_stdout or sys.stdout
        try:
            for line in record.getMessage().split('\n'):
                stream.write(f'<KPL>[{color}{kpl_level}{reset}] '
                             f'<\033[94m{self._node.name}{reset}> {line}\n')
            stream.flush()
        except Exception:
            pass


class _PrintCapture:
    """Replaces sys.stdout/sys.stderr inside the node subprocess so a bare
    print() (or an uncaught library warning on stderr) is captured and
    logged like a self.log() call, instead of becoming an untagged raw line
    that only reaches the terminal in debug mode and never reaches the log
    file. Line-buffers (logging records are per-line, not per-write()) and
    only ever calls into the logger — never touches sys.stdout itself — so
    it cannot recurse regardless of what sys.stdout currently is."""

    def __init__(self, logger, level, kpl_level):
        self._logger = logger
        self._level = level
        self._kpl_level = kpl_level
        self._buffer = ''

    def write(self, text):
        self._buffer += text
        while '\n' in self._buffer:
            line, self._buffer = self._buffer.split('\n', 1)
            if line:
                self._logger.log(self._level, line, extra={'kpl_level': self._kpl_level})
        return len(text)

    def flush(self):
        if self._buffer:
            self._logger.log(self._level, self._buffer, extra={'kpl_level': self._kpl_level})
            self._buffer = ''

    def isatty(self):
        return False


class SourceNodeRef:
    """Handle to the upstream node that produced a `nodes.X.Y`-reference input,
    exposed to node.py code as `self.inputs['name'].source_node` — identity
    only (`.name`), plus the lazy cache read backing `.value`. Deliberately
    does NOT expose control over the upstream node (no clear_cache/rerun):
    letting a node reach backward and force its own dependency to redo work
    mid-run questions the pipeline's declarative nodes.X.Y/DAG model, not just
    the plumbing. Only ever constructed inside a running node subprocess
    (KNode._init_argparser)."""

    def __init__(self, node_id, output_name, owner):
        self.name = node_id
        self._output_name = output_name
        self._owner = owner

    def get_value(self):
        """Read this input's current value from the source node's cache.
        Reuses KParameter.get_cached_value() rather than re-implementing the
        outputs.yaml read."""
        probe = KParameter(self._output_name)
        return probe.get_cached_value(self._owner.uuid, self.name, self._owner.cache_dir)


class GroupPortDict:
    """self.groups['slot_name'].inputs / .outputs — dict-like access to one
    bound group's declared ports, backed by the {name: ...} mapping the
    parent resolved (_group_input_map/_group_output_map in kpipeline.py, via
    _resolve_underlying_groups). Inputs are settable (staged; GroupRef.run()
    applies them as one-off `kpl run <inner_node> --<inner_input> <value>`
    overrides — never written to graph_config.yaml, exactly like any
    existing `kpl run <node> --key value` CLI override). Outputs are
    read-only, lazily read from cache after a run — reuses
    KParameter.get_cached_value(), same mechanism as SourceNodeRef."""

    def __init__(self, port_map, owner, readonly, group_ref):
        self._port_map = port_map or {}
        self._owner = owner
        self._readonly = readonly
        self._group_ref = group_ref
        self._staged = {}

    def __contains__(self, name):
        return name in self._port_map

    def keys(self):
        return list(self._port_map.keys())

    def __getitem__(self, name):
        # Checked here, not just in GroupRef.run(): an unbound group has no
        # port_map at all, so without this an access would surface as a
        # confusing "not a declared port" instead of the real problem.
        self._group_ref._require_bound()
        if name not in self._port_map:
            raise KeyError(f"'{name}' is not a declared port on this group")
        if self._readonly:
            node_id, port_name = self._port_map[name]
            probe = KParameter(port_name)
            return probe.get_cached_value(self._owner.uuid, node_id, self._owner.cache_dir)
        return self._staged.get(name)

    def __setitem__(self, name, value):
        self._group_ref._require_bound()
        if self._readonly:
            raise TypeError('group outputs are read-only')
        if name not in self._port_map:
            raise KeyError(f"'{name}' is not a declared input on this group")
        self._staged[name] = value


class GroupRef:
    """self.groups['slot_name'] inside node.py — only for a node type that
    declares a matching slot in its node_config.yaml's `underlying_groups`,
    bound (via this node's Properties panel) to a real group in this
    pipeline. Lets a node treat that group like a subroutine: stage input
    overrides, run it (blocking, like `kpl group run`), read its outputs,
    and clear its cache (blocking, like `kpl group clean`).

    This DOES expose control over another part of the graph — unlike
    SourceNodeRef above (deliberately read-only: an upstream `nodes.X.Y`
    reference is an IMPLICIT relationship the pipeline's declarative model
    already owns the execution order of). A node's underlying_groups is the
    opposite: an EXPLICIT, node-type-declared relationship to a group the
    node itself is meant to drive — closer to calling a subroutine than
    reaching backward into an unrelated dependency, which is why control
    (run/set-inputs) is the point here rather than something to avoid.

    Only ever constructed inside a running node subprocess
    (KNode._init_argparser)."""

    def __init__(self, name, info, owner):
        self.name = name
        self.group_id = info.get('group_id')
        self._owner = owner
        self.inputs = GroupPortDict(info.get('inputs'), owner, readonly=False, group_ref=self)
        self.outputs = GroupPortDict(info.get('outputs'), owner, readonly=True, group_ref=self)

    def _require_bound(self):
        if not self.group_id:
            raise RuntimeError(
                f"Group '{self.name}' is not bound to any group in this "
                f"pipeline yet — bind it in this node's Properties panel."
            )

    def run(self):
        """Blocking: `kpl group run <group_id> --set name=value ...` for any
        staged self.inputs[...] = value overrides — a SINGLE call, resolved
        by run_group() itself (kpipeline.py) to whichever inner node/port
        each override actually feeds. Deliberately not two separate calls
        (apply overrides, then run the group): when the override's target
        node is ALSO one of the group's own output-producing nodes (a
        single-node group, a common shape) that would run it twice, and the
        second (un-overridden) run would silently stomp the first's result —
        run_group() folds the override into that same node's one run instead.
        Clears staged inputs afterward either way. Returns True on success,
        False on failure."""
        self._require_bound()
        try:
            cmd = ['kpl', 'group', 'run', self.group_id]
            for name, value in self.inputs._staged.items():
                cmd += ['--set', f'{name}={value}']
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                # Without this, a GroupRef.run() failure from inside a kwui
                # node is a silent False with zero diagnostic — the inner
                # `kpl group run` subprocess's own stdout/stderr (which node
                # in the group actually failed, and why) went to whatever fd
                # this process inherited, never to the caller. Print AND log
                # it so it's visible in both a plain terminal run and a kwui
                # session's run.log.
                output = (r.stdout or '') + (r.stderr or '')
                print(f"[GroupRef.run] 'kpl group run {self.group_id}' failed "
                      f"(exit {r.returncode}):\n{output}", flush=True)
                if hasattr(self._owner, 'log'):
                    self._owner.log(
                        f"GroupRef.run('{self.group_id}') failed (exit {r.returncode}): {output}",
                        level='error',
                    )
            return r.returncode == 0
        finally:
            self.inputs._staged.clear()

    def remove_cache(self):
        """Blocking: `kpl group clean <group_id>` — clears the cache of
        every member node in this group (frozen members are skipped, same as
        `kpl group clean` on the CLI). Returns True on success, False on
        failure (e.g. the group no longer resolves)."""
        self._require_bound()
        r = subprocess.run(['kpl', 'group', 'clean', self.group_id])
        return r.returncode == 0


class KNode:
    def __init__(self, name=None, node_type=None, arg_parser=False, verbose=False, externals=None):
        self.name = name
        self.type = node_type
        self._verbose = verbose
        self.inputs = {}
        self.outputs = {}
        self.groups = {}
        self.state = NodeState.PENDING
        self.error = None
        self.description = ''
        self.category = 'General'
        self.env_manager = None
        self.env_directory = '.venv'
        self.requirements = None
        self.log_level = 'INFO'
        self.kwui = False
        self.ui = None
        self._logger = None
        self._real_stdout = None
        self._real_stderr = None
        self.gui = {'position': [0, 0], 'color': None}

        # Determine node directory
        caller_file = inspect.stack()[1].filename
        self.node_dir = os.path.dirname(os.path.abspath(caller_file))

        if node_type is not None:
            from kpipeline.src.kbase_nodes import find_node_dir
            # knode.py is at <root>/kpipeline/src/knode.py  →  go up 3 levels
            pipeliner_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            resolved = find_node_dir(node_type, pipeliner_dir)
            if resolved:
                self.node_dir = resolved

        # Load externals
        if externals is not None:
            self.externals = externals
        else:
            self.externals = self._load_externals()

        self.uuid = self.externals.get('uuid', '123456789') if self.externals else '123456789'

        # Load node config from the node directory
        config_path = os.path.join(self.node_dir, 'node_config.yaml')
        if os.path.exists(config_path):
            self._load_from_yaml(config_path)

        if externals is not None:
            self.uuid = externals.get('uuid', self.uuid)
            self.name = externals.get('node_id', self.name)

        if self.name:
            self._make_output_dir()

        if arg_parser:
            self._init_argparser()

        if hasattr(self, 'run'):
            try:
                self.run()
            except Exception:
                self.log(traceback.format_exc(), 'error')
                self._teardown_logging()
                sys.exit(1)   # not `raise` — self.log() above already put the
                               # traceback through the file + <KPL> terminal
                               # output; re-raising would make Python's default
                               # excepthook print the SAME traceback a second
                               # time (raw, untagged) after teardown restores
                               # the real stderr. sys.exit(1) still fails the
                               # node with a non-zero exit code, same as an
                               # uncaught exception would have.
            else:
                self._teardown_logging()

    def __str__(self):
        return f"KNode(name={self.name}, type={self.type})"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_externals(self):
        externals = {}
        extern_dir = os.path.join(os.getcwd(), 'extern')
        if not os.path.exists(extern_dir):
            return externals
        for fname in os.listdir(extern_dir):
            if not fname.endswith('.yaml'):
                continue
            path = os.path.join(extern_dir, fname)
            name = os.path.splitext(fname)[0]
            try:
                data = yaml.safe_load(open(path)) or {}
                if 'value' in data:
                    externals[name] = data['value']
            except Exception:
                pass
        return externals

    def _load_from_yaml(self, yaml_path):
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f)
        if not cfg:
            return

        self.name = cfg.get('name', self.name)
        self.type = cfg.get('type', self.type)
        self.description = cfg.get('description', '')
        self.category = cfg.get('category', 'General')
        self.kwui = bool(cfg.get('kwui', False))
        # Declared group SLOTS for this node TYPE — same shape as self.inputs/
        # self.outputs above: a name declared here, a per-INSTANCE value
        # merged in from graph_config.yaml's `groups: [{name, value}]` list
        # by KGraph.load_from_yaml (exactly like it already merges `inputs`
        # values into KParameter.value, right below where that happens).
        # value = the bound group's id, or None if this slot isn't bound yet
        # (bound via this node's Properties panel). Reachable from this
        # node's own code as self.groups[name] — see GroupRef in this file
        # (which is what self.groups actually holds once running as a
        # subprocess — richer than a plain KParameter, see there) and
        # kpipeline.py's _resolve_underlying_groups.
        # Same declaration shape as inputs/outputs above ({name: ...} dicts),
        # but tolerate a bare string too (accepted since this was introduced,
        # and cheap to keep accepting).
        _group_slot_names = [
            (g.get('name') if isinstance(g, dict) else g)
            for g in (cfg.get('underlying_groups', []) or [])
        ]
        self.groups = {name: KParameter(name) for name in _group_slot_names if name}

        py_env = cfg.get('python_environment', {}) or {}
        self.env_manager = py_env.get('env_manager')
        self.env_directory = py_env.get('env_directory', '.venv')
        self.requirements = py_env.get('requirements')

        for item in cfg.get('inputs', []) or []:
            name = item['name']
            value = item.get('default', item.get('Default'))
            param = KParameter(
                name, value,
                ptype=item.get('type'),
                format=item.get('format'),
                description=item.get('description')
            )
            self.inputs[name] = param

        for item in cfg.get('outputs', []) or []:
            name = item['name']
            param = KParameter(
                name, item.get('default'),
                ptype=item.get('type'),
                format=item.get('format'),
                description=item.get('description')
            )
            self.outputs[name] = param

    def _make_output_dir(self, cache_dir=None, data_dir=None):
        current_dir = os.getcwd()
        data_dir = data_dir or 'data'
        cache_dir = cache_dir or 'cache'
        self.data_dir = data_dir if os.path.isabs(data_dir) else os.path.join(current_dir, data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.cache_dir = cache_dir if os.path.isabs(cache_dir) else os.path.join(current_dir, cache_dir)
        self.output_dir = os.path.join(self.cache_dir, self.uuid, self.name)
        os.makedirs(self.output_dir, exist_ok=True)
        return self.output_dir

    def _setup_logging(self):
        """Wires self.log()/print()/uncaught-exceptions to a per-run log file
        under output_dir/log/, in addition to the existing <KPL> terminal/GUI
        output. self.log_level (set from graph_config.yaml's `log_level` via
        the --log_level CLI arg, pipeline-wide — not per node) is the single
        threshold for both destinations."""
        log_dir = os.path.join(self.output_dir, 'log')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'run.log')

        self._real_stdout = sys.stdout
        self._real_stderr = sys.stderr

        self._logger = logging.getLogger(f'kpl.node.{self.uuid}.{self.name}')
        self._logger.handlers.clear()  # idempotency safety net
        self._logger.propagate = False

        requested = (self.log_level or 'INFO').strip().upper()
        numeric_level = _LEVEL_NAME_TO_NUM.get(requested)
        invalid_level = None if numeric_level is not None else self.log_level
        self._logger.setLevel(numeric_level if numeric_level is not None else logging.INFO)

        file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
        file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        sep = f"===== RUN START pid={os.getpid()} {datetime.now().isoformat(timespec='seconds')} ====="
        file_handler.stream.write(sep + '\n')
        file_handler.stream.flush()
        self._logger.addHandler(file_handler)

        self._logger.addHandler(_KPLTerminalHandler(self))

        if invalid_level:
            self._logger.warning(f"Invalid log_level '{invalid_level}' in graph_config.yaml, "
                                  f"using INFO.", extra={'kpl_level': 'warning'})

        sys.stdout = _PrintCapture(self._logger, logging.INFO, 'info')
        sys.stderr = _PrintCapture(self._logger, logging.ERROR, 'error')

    def _teardown_logging(self):
        if self._logger is None:
            return
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        if self._real_stdout is not None:
            sys.stdout = self._real_stdout
        if self._real_stderr is not None:
            sys.stderr = self._real_stderr
        for h in list(self._logger.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            self._logger.removeHandler(h)

    def _init_argparser(self):
        p = argparse.ArgumentParser(prog=self.name or 'node',
                                    description=f'Node of type {self.type}')
        type_map = {'INT': int, 'FLOAT': float}
        for name, param in self.inputs.items():
            if param.type == 'BOOL':
                # BooleanOptionalAction (adds --name / --no-name) instead of a
                # plain store_true flag: a bare store_true flag has no way to
                # explicitly pass False — the caller can only omit the flag,
                # which falls back to THIS default (the node type's declared
                # default), silently ignoring a pipeline's explicit `false`
                # override whenever that default is `true`.
                p.add_argument(f'--{name}', action=argparse.BooleanOptionalAction, default=param.value)
            else:
                p.add_argument(f'--{name}',
                               type=type_map.get(param.type, str),
                               default=param.value)
        p.add_argument('--uuid', default='123456789')
        p.add_argument('--node_id', required=True)
        p.add_argument('--node_type', default=None)
        p.add_argument('--cache_dir', default='cache')
        p.add_argument('--data_dir', default='data')
        p.add_argument('--log_level', default='INFO')
        p.add_argument('--no_file_log', action='store_true')
        p.add_argument('--kwui_bridge', action='store_true')
        # Reserved input name: {input_name: {"node": ..., "output": ...}} for
        # every input whose value is a `nodes.X.Y` reference. The parent no
        # longer resolves these to a literal value before spawning us (it
        # still runs the source node first if it isn't cached yet - only the
        # value READ moves here); see kpipeline.py's _run_node_inner.
        p.add_argument('--input_sources', default='{}')
        # {slot_name: {"group_id": id-or-null, "inputs": {...}, "outputs": {...}}}
        # for every group slot this node type declares (underlying_groups in
        # its node_config.yaml), resolved by the parent against THIS
        # pipeline's actual groups/group_links; see kpipeline.py's
        # _resolve_underlying_groups. Empty for every node type that
        # declares no slots (the default).
        p.add_argument('--groups', default='{}')
        args = p.parse_args()

        self.uuid = args.uuid
        self.name = args.node_id
        if args.node_type:
            self.type = args.node_type

        ref_sources = json.loads(args.input_sources or '{}')
        for name, info in ref_sources.items():
            param = self.inputs.get(name)
            if param is not None:
                param.set_source_node(SourceNodeRef(info['node'], info['output'], self))

        for name, param in self.inputs.items():
            if name in ref_sources:
                continue
            if hasattr(args, name):
                param.value = getattr(args, name)

        # Upgrades self.groups from the plain {name: KParameter(value=bound_id)}
        # dict _load_from_yaml built (fine for the orchestrator, which only
        # ever needs the declared name + bound id) into real GroupRef objects
        # — the run()/inputs/outputs capability only makes sense inside an
        # actual running node subprocess, which is exactly where we are now.
        groups_info = json.loads(args.groups or '{}')
        self.groups = {name: GroupRef(name, info, self) for name, info in groups_info.items()}

        self._make_output_dir(cache_dir=args.cache_dir, data_dir=args.data_dir)
        self.log_level = args.log_level
        # graph_config.yaml's `log_enabled: false` -> --no_file_log: skip
        # setup entirely, so log()/send_status_message() fall back to their
        # pre-existing raw-print behavior (no file, no stdout/stderr capture).
        if not args.no_file_log:
            self._setup_logging()
        # After _setup_logging(): UIProxy writes to the saved _real_stdout.
        if self.kwui:
            from kpipeline.src.kwui import NoopUI, UIProxy
            self.ui = UIProxy(self) if args.kwui_bridge else NoopUI()
        return args

    # ------------------------------------------------------------------
    # Output helpers (called from inside node.py)
    # ------------------------------------------------------------------

    def make_dir(self, subdir):
        path = os.path.join(self.output_dir, subdir)
        os.makedirs(path, exist_ok=True)
        return path

    def save_output(self, out_name, value, verbose=True):
        outputs_yaml = os.path.join(self.output_dir, 'outputs.yaml')
        existing = {}
        if os.path.exists(outputs_yaml):
            with open(outputs_yaml) as f:
                existing = yaml.safe_load(f) or {}
        existing[out_name] = value
        with open(outputs_yaml, 'w') as f:
            yaml.dump(existing, f)
        if out_name in self.outputs:
            self.outputs[out_name].value = value
        if verbose:
            self.send_status_message(status='RESULT', message=json.dumps({out_name: value}))

    def save_image(self, out_name, image):
        """Save a numpy image (via cv2) to the output dir."""
        try:
            import cv2
            path = os.path.join(self.output_dir, f'{out_name}.jpg')
            cv2.imwrite(path, image)
            self.save_output(out_name, path)
        except ImportError:
            self.log('cv2 not available for save_image', 'warning')

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def send_status_message(self, status='none', progress=0, max=100, message=''):
        data = {'status': status, 'progress': progress, 'max': max, 'message': message}
        # Write straight to the SAVED real stdout, never through print()/the
        # current sys.stdout: once _setup_logging() has installed
        # _PrintCapture there, a bare print() here would get swallowed into
        # the log file as an "info" line instead of reaching the parent's
        # <KPM> parser, breaking progress bars and save_output's RESULT relay.
        # Bypass-path objects (no _setup_logging() call) never set
        # _real_stdout, so this falls back to sys.stdout — identical to today.
        stream = getattr(self, '_real_stdout', None) or sys.stdout
        stream.write(f'<KPM> {json.dumps(data)} </KPM>\n')
        stream.flush()

        # Also record it in the log file (kpl_level='status' makes
        # _KPLTerminalHandler skip it — it's already visible above, on the
        # real terminal, via the <KPM> line itself/progress bars; no need
        # for a second, differently-formatted echo of the same event there).
        if getattr(self, '_logger', None) is not None:
            self._logger.info(
                f'STATUS status={status} progress={progress}/{max} message={message}',
                extra={'kpl_level': 'status'})

    def log(self, message, level='info'):
        if getattr(self, '_logger', None) is not None:
            numeric = _KPL_LEVEL_TO_NUM.get(level, logging.INFO)
            self._logger.log(numeric, message, extra={'kpl_level': level})
            return
        # Bypass path (KNode.__new__ objects — e.g. the parent-process runner
        # relaying a RESULT message via execute(), or log_enabled: false)
        # never calls _setup_logging(): preserve today's raw-print behavior,
        # just reusing the same color table _KPLTerminalHandler uses so the
        # new 'debug' level is styled consistently either way.
        color = _KPL_COLORS.get(level, '')
        reset = '\033[0m'
        print(f'<KPL>[{color}{level}{reset}] <\033[94m{self.name}{reset}> {message}', flush=True)

    # ------------------------------------------------------------------
    # Execution (called from the graph runner, not from inside node.py)
    # ------------------------------------------------------------------

    def _default_python(self):
        """Python used when a node declares no environment of its own: the
        interpreter kpl itself is running under. That's already resolved to
        the installation's configured `python_env` (config.yaml) — or, if
        none is configured, the default python environment kpl was invoked
        with — by kpipeline.py's `_ensure_venv_python()` at CLI startup, so
        reusing `sys.executable` here (rather than re-deriving it) keeps a
        single source of truth instead of a second, possibly stale one."""
        return sys.executable or 'python'

    def _resolve_env_dir(self):
        """self.env_directory, anchored at this node's own directory.

        A node runs as a subprocess launched from wherever the pipeline
        itself was invoked (the pipeline dir), not from node_dir - so a
        relative env_directory must be resolved against node_dir explicitly
        here, rather than left for the OS to resolve against the wrong
        working directory. '.venv' (single node's own env) and '../.venv'
        (shared with sibling node types in the parent/category directory)
        both fall out of this the same way; an absolute path is returned
        unchanged.
        """
        env_dir = self.env_directory
        if not os.path.isabs(env_dir):
            env_dir = os.path.normpath(os.path.join(self.node_dir, env_dir))
        return env_dir

    def _python_prefix(self):
        if not self.env_manager:
            return self._default_python()
        if self.env_manager == 'venv':
            env_dir = self._resolve_env_dir()
            prefix = os.path.join(env_dir, 'bin', 'python')
            if not os.path.exists(prefix):
                raise FileNotFoundError(
                    f'venv not found at {prefix}. Run: kpl node_env install'
                )
            return prefix
        if self.env_manager == 'conda':
            env_dir = self._resolve_env_dir()
            return os.path.join(env_dir, 'bin', 'python3')
        if self.env_manager == 'pixi':
            return f'pixi run --manifest-path {self.env_directory}/pixi.toml python'
        return self._default_python()

    def execute(self, params=None, show_progress=True, show_kpl=True,
                show_kpm=False, debug=False, line_callback=None):
        """
        Execute this node as a subprocess.

        Returns 0 on success, non-zero on failure.
        line_callback(line: str) is called for each stdout line (used by GUI).
        """
        if params is None:
            params = {}

        # uuid, node_id, node_type are already in the base command – skip them
        _skip = {'uuid', 'node_id', 'node_type'}
        in_params = ''
        for key, value in params.items():
            if key in _skip:
                continue
            param = self.inputs.get(key)
            if param is not None and (param.type or '').upper() == 'BOOL':
                # argparse declares BOOL inputs via BooleanOptionalAction
                # (--key / --no-key): always pass one of the two explicitly.
                # Omitting the flag on a falsy value (as a plain store_true
                # flag would require) would fall back to the node type's
                # declared default instead of this explicit override.
                truthy = value is True or str(value).strip().lower() in ('true', '1', 'yes')
                in_params += f'--{key} ' if truthy else f'--no-{key} '
                continue
            if value is not None and str(value).strip() != '':
                in_params += f'--{key}={shlex.quote(str(value))} '

        try:
            python_prefix = self._python_prefix()
        except FileNotFoundError as e:
            self.log(str(e), 'error')
            return 1

        uuid_val = params.get('uuid', self.uuid)
        node_id_val = params.get('node_id', self.name)
        cache_dir_val = getattr(self, 'cache_dir', None) or 'cache'
        data_dir_val = getattr(self, 'data_dir', None) or 'data'
        log_level_val = getattr(self, 'log_level', None) or 'INFO'
        no_file_log_flag = '' if getattr(self, 'log_enabled', True) else '--no_file_log '

        # kwui mode: only wire up the bridge for a node that declares
        # `kwui: true` AND a live workflow UI to talk to. Without both, nothing
        # below changes — no --kwui_bridge flag, no stdin pipe, no repeat loop.
        bridge = None
        run_env = None
        if getattr(self, 'kwui', False):
            from kpipeline.src.kwui import (STATUS_ERROR, STATUS_REPEAT, WIDGETS_ENV,
                                            available_widget_names, get_active_bridge)
            bridge = get_active_bridge()
            # Let the node validate self.ui.<name> against the real widget list
            # (it may run in a venv without kwebui, so it can't look it up).
            widget_names = available_widget_names()
            if widget_names:
                run_env = dict(os.environ, **{WIDGETS_ENV: ','.join(widget_names)})

        cmd = (
            f'{python_prefix} {self.node_dir}/node.py '
            f'--uuid {uuid_val} '
            f'--node_id {node_id_val} '
            f'--node_type {self.type} '
            f'--cache_dir "{cache_dir_val}" '
            f'--data_dir "{data_dir_val}" '
            f'--log_level "{log_level_val}" '
            f'{no_file_log_flag}'
            f'{"--kwui_bridge " if bridge is not None else ""}'
            f'{in_params.strip()}'
        )

        if debug:
            print(f'[DEBUG] {cmd}', flush=True)

        while True:
            process = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if bridge is not None else None,
                env=run_env,   # None -> inherit, exactly as before kwui existed
                universal_newlines=True
            )
            if bridge is not None:
                bridge.attach(self.name, process.stdin)

            progress_bars = {}
            try:
                while True:
                    line = process.stdout.readline()
                    if line == '' and process.poll() is not None:
                        break
                    if not line:
                        continue
                    line = line.rstrip()

                    if '<KWUI>' in line:
                        # UI traffic, not node output: never printed and never
                        # relayed to line_callback (the GUI would show raw JSON).
                        if bridge is not None:
                            bridge.handle_line(self.name, line)
                        continue

                    if line_callback:
                        line_callback(line)

                    is_kpm = '<KPM>' in line
                    is_kpl = '<KPL>' in line

                    if is_kpm:
                        parsed = self._parse_kpm(line)
                        if parsed:
                            status = parsed.get('status', '')
                            if status == 'RUNNING' and show_progress:
                                self._update_progress_bar(parsed, progress_bars)
                            if show_kpm:
                                print(f'KPM: {parsed}', flush=True)
                            if status in ('COMPLETED', 'FAILED', 'RESULT'):
                                for pb in progress_bars.values():
                                    pb.close()
                                progress_bars.clear()
                            if status == 'RESULT':
                                msg = self._parse_message(parsed.get('message', ''))
                                if msg:
                                    self.log(str(msg), 'output')
                    elif is_kpl:
                        if show_kpl or debug:
                            print(line, flush=True)
                    else:
                        if debug:
                            print(line, flush=True)
            finally:
                for pb in progress_bars.values():
                    pb.close()
                if bridge is not None:
                    bridge.detach(self.name)
                    try:
                        process.stdin.close()
                    except Exception:
                        pass

            ret = process.poll()
            if bridge is None:
                return ret

            ui_status = bridge.consume_status(self.name)
            if ret == 0 and ui_status == STATUS_REPEAT:
                self.log('kwui: repeating node on request', 'info')
                # Same reset a fresh RUNNING state does: drop the previous
                # run's widgets (and show the "Working ..." placeholder) so
                # the repeated run starts from a clean page.
                try:
                    bridge.on_node_state(self.name, 'RUNNING')
                except Exception:
                    pass
                continue
            if ret == 0 and ui_status == STATUS_ERROR:
                return 1
            return ret

    def install_env(self):
        """Install the node's Python environment."""
        if not self.env_manager or self.env_manager == 'null':
            print('No environment manager specified.')
            return True

        if self.env_manager == 'venv':
            env_dir = self._resolve_env_dir()
            if not os.path.exists(env_dir):
                print(f'Creating venv at {env_dir}...')
                subprocess.run([sys.executable, '-m', 'venv', env_dir], check=True)
            pip = os.path.join(env_dir, 'bin', 'pip')
            if self.requirements:
                req_file = os.path.join(self.node_dir, self.requirements)
                if os.path.exists(req_file):
                    print(f'Installing requirements from {req_file}...')
                    subprocess.run([pip, 'install', '-r', req_file], check=True)
            return True

        print(f'Environment manager "{self.env_manager}" install not automated. Please set it up manually.')
        return False

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_kpm(self, line):
        if '<KPM>' not in line:
            return None
        try:
            data = line.split('<KPM> ')[1].split(' </KPM>')[0].strip()
            return json.loads(data)
        except Exception:
            return None

    def _parse_message(self, message):
        try:
            return json.loads(message)
        except Exception:
            return message

    def _update_progress_bar(self, data, bars):
        from tqdm import tqdm
        status = data.get('status', 'RUNNING')
        progress = data.get('progress', 0)
        max_val = data.get('max', 100)
        message = data.get('message', '')

        for k in list(bars.keys()):
            if k != status:
                bars[k].close()
                del bars[k]

        if status not in bars:
            bars[status] = tqdm(
                total=max_val,
                desc=f'{self.name}',
                position=0, leave=True,
                bar_format='{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}] {postfix}',
                file=sys.stdout
            )

        pb = bars[status]
        pb.total = max_val
        pb.n = progress
        pb.set_postfix_str(message)
        pb.refresh()

    # ------------------------------------------------------------------
    # Output value access
    # ------------------------------------------------------------------

    def get_output_value(self, out_name):
        param = self.outputs.get(out_name)
        return param.value if param else None
