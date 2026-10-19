"""
Base-nodes directory handling.

The default base-nodes directory is configured in <pipeliner_root>/config.yaml:

    default_base_nodes_dir: base_nodes   # relative to the pipeliner root, or absolute
    (legacy key `base_nodes` is still honored)

A pipeline can additionally bring its own node sources via graph_config.yaml:

    load_default_base_nodes_dir: true    # include the default dir (default: true)
    custom_base_nodes_dirs: []           # extra dirs, relative to the pipeline dir
                                         # or absolute; scanned BEFORE the default,
                                         # so a custom node type wins on name clash

Inside each base-nodes directory, nodes are organized in category directories:

    BASE_NODES/
      io nodes/            <- category "io nodes"
        pwz_loader/
          node_config.yaml
          node.py
      _experimental/       <- ignored (starts with '_')
        some_node/
      example_add/         <- node directly in the root: category "no category"
        node_config.yaml

A directory is a node if it contains a node_config.yaml; otherwise it is
treated as a category directory whose children are nodes.
"""

import os
import yaml

DEFAULT_BASE_NODES = 'base_nodes'
DEFAULT_GROUP_BASE_NODES = 'group_base_nodes'
NO_CATEGORY = 'no category'


def _dir_from_config(pipeliner_dir, keys, default):
    """Resolve a directory setting from config.yaml (relative to the
    pipeliner root, or absolute). `keys` may list fallback keys (legacy names)."""
    if isinstance(keys, str):
        keys = [keys]
    cfg_path = os.path.join(pipeliner_dir, 'config.yaml')
    base = default
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            base = next((cfg[k] for k in keys if cfg.get(k)), default)
        except Exception:
            pass
    base = os.path.expanduser(str(base))
    if not os.path.isabs(base):
        base = os.path.join(pipeliner_dir, base)
    return base


def get_default_base_nodes_dir(pipeliner_dir):
    """Resolve the default base-nodes directory from config.yaml
    (key: default_base_nodes_dir; legacy key `base_nodes` still honored)."""
    return _dir_from_config(pipeliner_dir, ['default_base_nodes_dir', 'base_nodes'],
                            DEFAULT_BASE_NODES)


# Backward-compatible alias (pre-2026-07-16 name).
get_base_nodes_dir = get_default_base_nodes_dir


def resolve_base_nodes_dirs(pipeliner_dir, load_default=True, custom_dirs=None,
                            pipeline_dir=None):
    """Effective, ordered list of base-nodes directories for a pipeline.

    Custom dirs come first (a custom node type overrides a default one of the
    same name); relative custom paths are resolved against the pipeline dir
    (default: cwd). The default dir is appended when load_default is true."""
    pipeline_dir = pipeline_dir or os.getcwd()
    dirs = []
    for d in custom_dirs or []:
        if not d:
            continue
        d = os.path.expanduser(str(d))
        if not os.path.isabs(d):
            d = os.path.join(pipeline_dir, d)
        d = os.path.normpath(d)
        if d not in dirs:
            dirs.append(d)
    if load_default:
        default = get_default_base_nodes_dir(pipeliner_dir)
        if default not in dirs:
            dirs.append(default)
    return dirs


def get_group_base_nodes_dir(pipeliner_dir):
    """Resolve the exported-group components directory from config.yaml
    (key: group_base_nodes) — the installation-wide fallback; a pipeline's own
    `groups_dir` (graph_config.yaml) is resolved by resolve_groups_dir."""
    return _dir_from_config(pipeliner_dir, 'group_base_nodes', DEFAULT_GROUP_BASE_NODES)


def resolve_groups_dir(pipeline_dir, configured):
    """Absolute path of a pipeline's `groups_dir` (graph_config.yaml: relative to the
    pipeline dir, or absolute), or None when the pipeline does not set one."""
    if not configured:
        return None
    path = os.path.expanduser(str(configured))
    if not os.path.isabs(path):
        path = os.path.join(pipeline_dir or os.getcwd(), path)
    return os.path.normpath(path)


def resolve_groups_dirs(pipeliner_dir, pipeline_dir, configured):
    """Ordered group-template directories to READ: the pipeline's `groups_dir` first (a
    group there overrides one of the same name below), then the installation-wide
    group_base_nodes dir, so groups exported before a pipeline had a `groups_dir` stay available."""
    dirs = []
    own = resolve_groups_dir(pipeline_dir, configured)
    if own:
        dirs.append(own)
    shared = get_group_base_nodes_dir(pipeliner_dir)
    if shared not in dirs:
        dirs.append(shared)
    return dirs


def scan_base_nodes(base_nodes_dirs):
    """
    Return {node_type: {'dir': <abs path>, 'category': <str>}}.

    Accepts a single directory or an ordered list of directories; on a name
    clash the EARLIER directory wins (setdefault). Categories starting with
    '_' (or '.') are skipped. Nodes sitting directly in a base-nodes root get
    the category "no category".
    """
    if isinstance(base_nodes_dirs, str):
        base_nodes_dirs = [base_nodes_dirs]
    nodes = {}

    def is_node(path):
        return os.path.isfile(os.path.join(path, 'node_config.yaml'))

    for base_nodes_dir in base_nodes_dirs:
        if not os.path.isdir(base_nodes_dir):
            continue
        for entry in sorted(os.listdir(base_nodes_dir)):
            if entry.startswith('_') or entry.startswith('.'):
                continue
            entry_path = os.path.join(base_nodes_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if is_node(entry_path):
                nodes.setdefault(entry, {'dir': entry_path, 'category': NO_CATEGORY})
            else:
                # Category directory: children are nodes under this category.
                for child in sorted(os.listdir(entry_path)):
                    if child.startswith('_') or child.startswith('.'):
                        continue
                    child_path = os.path.join(entry_path, child)
                    if os.path.isdir(child_path) and is_node(child_path):
                        nodes.setdefault(child, {'dir': child_path, 'category': entry})
    return nodes


def find_node_dir(node_type, pipeliner_dir, dirs=None):
    """Return the directory of a node type, or None if not installed.

    `dirs`: optional ordered list of base-nodes dirs (e.g. a pipeline's
    effective dirs). Defaults to the installation's default dir only —
    base_node install/uninstall manage that dir and must not resolve into
    pipeline-specific custom dirs."""
    base = dirs if dirs is not None else get_default_base_nodes_dir(pipeliner_dir)
    info = scan_base_nodes(base).get(node_type)
    return info['dir'] if info else None
