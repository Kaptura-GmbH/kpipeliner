"""
MCP tool implementations for kpipeliner.

Pure, transport-agnostic operations on a pipeline directory — no dependency
on the `mcp` SDK or FastAPI. kpipeline_gui/mcp_app.py wraps these as MCP
tools and mounts them on the GUI's FastAPI app (`kpl --mcp` / `kpl gui --mcp`).

Anything the `kpl` CLI already does (add/run/status/tree/init) is executed
by shelling out to it, exactly like kpipeline_gui/server.py already does for
runs — this file adds new logic only where no CLI command exists yet
(editing node inputs/connections, creating or editing a node's own files).

Safety boundary: `write_node_file` only ever touches node types that resolve
under the pipeline's OWN `custom_base_nodes_dirs` — never the shared,
install-wide `default_base_nodes_dir`, since that directory is used by other
pipelines too.
"""

import os
import re
import shutil
import subprocess
import sys

import yaml

from kpipeline.src.kpaths import PIPELINER_DIR, config_dir, kpl_env, kpl_python_command
ALLOWED_NODE_FILES = ('node.py', 'node_config.yaml')


def _kpl(args, workdir):
    """Run `kpl <args>` as a subprocess in workdir, capturing output."""
    proc = subprocess.run(
        kpl_python_command() + list(args),
        cwd=workdir, capture_output=True, text=True, env=kpl_env(),
    )
    return {'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}


def _graph_config_path(workdir):
    return os.path.join(workdir, 'graph_config.yaml')


def _load_graph(workdir):
    from kpipeline.src.kgraph import KGraph
    graph = KGraph(verbose=False)
    graph.load_externals()
    cfg_path = _graph_config_path(workdir)
    if os.path.exists(cfg_path):
        graph.load_from_yaml(cfg_path)
    return graph


def _save_graph(graph, workdir):
    graph.save_to_yaml(_graph_config_path(workdir))


# ---------------------------------------------------------------------------
# Pipeline lifecycle / inspection (delegate to `kpl`)
# ---------------------------------------------------------------------------

def init_pipeline(workdir):
    return _kpl(['init'], workdir)


def get_status(workdir):
    return _kpl(['status'], workdir)


def get_tree(workdir):
    return _kpl(['tree'], workdir)


def run_pipeline(workdir, node_id=None, debug=False, use_cache=True):
    args = ['run_debug' if debug else 'run']
    if node_id:
        args.append(node_id)
    if not use_cache:
        args.append('--no-use-cache')
    return _kpl(args, workdir)


# ---------------------------------------------------------------------------
# Graph inspection
# ---------------------------------------------------------------------------

def list_node_types(workdir):
    graph = _load_graph(workdir)
    return graph.list_available_node_types()


def get_graph(workdir):
    graph = _load_graph(workdir)
    return {
        'name': graph.name,
        'description': graph.description,
        'inputs': graph.input_nodes,
        'outputs': graph.output_nodes,
        'custom_base_nodes_dirs': graph.custom_base_nodes_dirs,
        'load_default_base_nodes_dir': graph.load_default_base_nodes_dir,
        'missing_nodes': graph.missing_nodes,
        'nodes': [
            {
                'name': n.name,
                'type': n.type,
                'category': n.category,
                'inputs': {k: v.value for k, v in n.inputs.items()},
                'outputs': list(n.outputs.keys()),
            }
            for n in graph.nodes
        ],
    }


# ---------------------------------------------------------------------------
# Graph editing (no CLI equivalent — direct graph_config.yaml edits via KGraph)
# ---------------------------------------------------------------------------

def add_node(workdir, node_type, node_id=None):
    args = ['add', node_type] + ([node_id] if node_id else [])
    return _kpl(args, workdir)


def remove_node(workdir, node_name):
    graph = _load_graph(workdir)
    if not graph.get_node(node_name):
        return {'error': f"Node '{node_name}' not found."}
    graph.remove_node(node_name)
    # Clear now-dangling `nodes.<removed>.*` references in other nodes' inputs.
    ref_prefix = f'nodes.{node_name}.'
    for n in graph.nodes:
        for p in n.inputs.values():
            if isinstance(p.value, str) and p.value.startswith(ref_prefix):
                p.value = ''
    graph.input_nodes = [n for n in graph.input_nodes if n != node_name]
    graph.output_nodes = [n for n in graph.output_nodes if n != node_name]
    _save_graph(graph, workdir)
    return {'removed': node_name}


def set_node_input(workdir, node_name, input_name, value):
    graph = _load_graph(workdir)
    node = graph.get_node(node_name)
    if not node:
        return {'error': f"Node '{node_name}' not found."}
    if input_name not in node.inputs:
        return {'error': f"Node '{node_name}' has no input '{input_name}'. "
                          f"Available inputs: {list(node.inputs)}"}
    node.inputs[input_name].value = value
    _save_graph(graph, workdir)
    return {'node': node_name, 'input': input_name, 'value': value}


def set_pipeline_io(workdir, inputs=None, outputs=None):
    """Set the pipeline's declared input/output node-name lists. `outputs`
    determines what `kpl run` with no explicit node actually executes."""
    graph = _load_graph(workdir)
    names = {n.name for n in graph.nodes}
    if inputs is not None:
        unknown = [n for n in inputs if n not in names]
        if unknown:
            return {'error': f'Unknown node(s) for inputs: {unknown}'}
        graph.input_nodes = list(inputs)
    if outputs is not None:
        unknown = [n for n in outputs if n not in names]
        if unknown:
            return {'error': f'Unknown node(s) for outputs: {unknown}'}
        graph.output_nodes = list(outputs)
    _save_graph(graph, workdir)
    return {'inputs': graph.input_nodes, 'outputs': graph.output_nodes}


# ---------------------------------------------------------------------------
# Node type creation / editing (pipeline-local custom_base_nodes_dirs only)
# ---------------------------------------------------------------------------

def _ensure_local_nodes_dir(workdir, graph):
    """Return the pipeline-local custom base-nodes dir (absolute path),
    creating one (./nodes) and registering it in graph_config.yaml if the
    pipeline doesn't have one yet."""
    if graph.custom_base_nodes_dirs:
        rel = graph.custom_base_nodes_dirs[0]
    else:
        rel = 'nodes'
        graph.custom_base_nodes_dirs = [rel]
        _save_graph(graph, workdir)
    abs_dir = rel if os.path.isabs(rel) else os.path.join(workdir, rel)
    os.makedirs(abs_dir, exist_ok=True)
    return abs_dir


def create_node_type(workdir, node_type, category=None, description=''):
    """Create a new node type's node.py + node_config.yaml (from the
    standard templates) inside the pipeline's own custom_base_nodes_dirs, so
    it's immediately available to add_node() in this pipeline only."""
    if not re.fullmatch(r'\w+', node_type):
        return {'error': 'node_type must match [A-Za-z0-9_]+'}
    graph = _load_graph(workdir)
    local_dir = _ensure_local_nodes_dir(workdir, graph)
    cat_dir = os.path.join(local_dir, category) if category else local_dir
    node_dir = os.path.join(cat_dir, node_type)
    if os.path.exists(node_dir):
        return {'error': f"'{node_type}' already exists at "
                          f"{os.path.relpath(node_dir, workdir)}"}
    os.makedirs(node_dir)
    templates_dir = os.path.join(PIPELINER_DIR, 'kpipeline', 'templates')
    shutil.copy2(os.path.join(templates_dir, 'config.node_template.yaml'),
                 os.path.join(node_dir, 'node_config.yaml'))
    shutil.copy2(os.path.join(templates_dir, 'node_template.py'),
                 os.path.join(node_dir, 'node.py'))
    if description:
        cfg_path = os.path.join(node_dir, 'node_config.yaml')
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        cfg['description'] = description
        with open(cfg_path, 'w') as f:
            yaml.dump(cfg, f, sort_keys=False)
    return {'created': node_type, 'dir': os.path.relpath(node_dir, workdir)}


def read_node_file(workdir, node_type, filename='node.py'):
    if filename not in ALLOWED_NODE_FILES:
        return {'error': f'filename must be one of {ALLOWED_NODE_FILES}'}
    from kpipeline.src.kbase_nodes import find_node_dir
    graph = _load_graph(workdir)
    node_dir = find_node_dir(node_type, config_dir(), dirs=graph.get_base_nodes_dirs())
    if not node_dir:
        return {'error': f"Node type '{node_type}' not found."}
    path = os.path.join(node_dir, filename)
    if not os.path.exists(path):
        return {'error': f"'{filename}' not found for '{node_type}'."}
    with open(path) as f:
        return {'node_type': node_type, 'filename': filename, 'content': f.read()}


def write_node_file(workdir, node_type, filename, content):
    """Only pipeline-local node types (this pipeline's own
    custom_base_nodes_dirs) can be edited this way — shared/default base
    nodes are intentionally read-only here since other pipelines use them."""
    if filename not in ALLOWED_NODE_FILES:
        return {'error': f'filename must be one of {ALLOWED_NODE_FILES}'}
    from kpipeline.src.kbase_nodes import find_node_dir, resolve_base_nodes_dirs
    graph = _load_graph(workdir)
    local_dirs = resolve_base_nodes_dirs(
        config_dir(), load_default=False,
        custom_dirs=graph.custom_base_nodes_dirs, pipeline_dir=workdir,
    )
    node_dir = find_node_dir(node_type, config_dir(), dirs=local_dirs)
    if not node_dir:
        return {'error': f"'{node_type}' is not a pipeline-local node type "
                          f"(only nodes under this pipeline's "
                          f"custom_base_nodes_dirs are writable via MCP; "
                          f"shared base nodes are read-only — use read_node_file "
                          f"to inspect them, or create_node_type to make a local copy)."}
    with open(os.path.join(node_dir, filename), 'w') as f:
        f.write(content)
    return {'node_type': node_type, 'filename': filename, 'written': True}
