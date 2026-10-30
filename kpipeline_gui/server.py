#!/usr/bin/env python3
"""
kpipeliner GUI – FastAPI backend.

Serves the React frontend and provides:
  GET  /api/nodes              – available node types
  GET  /api/graph              – current graph_config.yaml as JSON
  POST /api/graph              – save graph from JSON
  POST /api/run                – start pipeline (or a specific node)
  POST /api/run-group          – run one group (`kpl group run <id>`)
  POST /api/stop               – stop current run
  GET  /api/status             – node states + system stats
  WS   /ws                     – real-time log + progress stream
  GET  /                       – React app (static)
"""

import argparse
import asyncio
import atexit
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINER_DIR = os.path.dirname(GUI_DIR)
FRONTEND_DIST = os.path.join(GUI_DIR, 'frontend', 'dist')
sys.path.insert(0, PIPELINER_DIR)

from kpipeline import KGraph  # noqa: E402
from kpipeline.src.kbase_nodes import (  # noqa: E402
    get_group_base_nodes_dir, resolve_base_nodes_dirs, resolve_groups_dir,
    resolve_groups_dirs, scan_base_nodes,
)
from kpipeline.src.kpaths import (  # noqa: E402
    config_dir, config_path, ensure_user_config, kpl_command, kpl_env, mcp_install_hint,
    read_configured_dirs,
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title='kpipeliner GUI')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Work directory set by CLI argument
_workdir: str = os.getcwd()
#: The address this server is bound to; the UI-designer servers it starts bind to the same one.
_bind_host: str = '127.0.0.1'

# Active WebSocket connections
_ws_clients: list[WebSocket] = []
_ws_lock = threading.Lock()

# Running process
_run_process: Optional[subprocess.Popen] = None
_run_lock = threading.Lock()
_node_states: dict = {}

# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------


def _broadcast(msg: dict):
    """Send a message to all connected WebSocket clients (thread-safe)."""
    data = json.dumps(msg)
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        asyncio.run_coroutine_threadsafe(_ws_send(ws, data), _loop)


async def _ws_send(ws: WebSocket, data: str):
    try:
        await ws.send_text(data)
    except Exception:
        pass


_loop: asyncio.AbstractEventLoop = None  # set at startup


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------


def _system_stats() -> dict:
    stats = {'cpu': 0.0, 'gpu': None, 'mem': 0.0}
    try:
        import psutil
        stats['cpu'] = psutil.cpu_percent(interval=None)
        stats['mem'] = psutil.virtual_memory().percent
    except ImportError:
        pass
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            vals = [float(v) for v in result.stdout.strip().split('\n') if v.strip()]
            if vals:
                stats['gpu'] = sum(vals) / len(vals)
    except Exception:
        pass
    return stats


def _external_running() -> list:
    """Node names currently RUNNING in a `kpl run` started OUTSIDE this GUI
    in the same workdir (read from cache/run_state.json, written by kpl on
    every state change), plus — for a kwui run whose nodes start nested `kpl`
    processes (groups) — the nodes those are running (cache/run_state_nested.json,
    one entry per live process). Purely informational — the GUI only highlights
    these nodes; its own run controls stay fully available.

    Returns [] when this GUI has a run of its own in progress. Otherwise the main
    file counts unless it is missing/unreadable, its writer was GUI-spawned
    (source != 'terminal') or that process is no longer alive (stale file, e.g.
    after Ctrl+C); nested entries count only while their process is alive."""
    with _run_lock:
        own_run = _run_process is not None and _run_process.poll() is None
    if own_run:
        return []
    names = set()
    path = os.path.join(_cache_root(), 'run_state.json')
    try:
        with open(path) as f:
            data = json.load(f) or {}
        pid = data.get('pid')
        if data.get('source') == 'terminal' and isinstance(pid, int):
            os.kill(pid, 0)  # raises OSError if the writer is gone
            names.update(n for n, s in (data.get('states') or {}).items() if s == 'RUNNING')
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        with open(os.path.join(_cache_root(), 'run_state_nested.json')) as f:
            procs = (json.load(f) or {}).get('procs') or {}
        for pid, entry in procs.items():
            try:
                os.kill(int(pid), 0)
            except PermissionError:
                pass  # exists, just not ours to signal
            except (OSError, ValueError):
                continue
            names.update(n for n, s in ((entry or {}).get('states') or {}).items() if s == 'RUNNING')
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        pass
    return sorted(names)


def _stats_loop():
    while True:
        time.sleep(2)
        stats = _system_stats()
        _broadcast({'type': 'system', **stats, 'node_states': _node_states,
                    'external_running': _external_running()})


# ---------------------------------------------------------------------------
# Process runner
# ---------------------------------------------------------------------------

_KPM_RE = re.compile(r'<KPM>\s*(\{.*?\})\s*</KPM>')
_KPL_RE = re.compile(r'<KPL>(.*)')
_KPS_RE = re.compile(r'<KPS>\s*(\{.*?\})\s*</KPS>')


def _kill_run_process():
    """Kill the current run process group with SIGKILL (cannot be ignored)."""
    global _run_process
    with _run_lock:
        proc = _run_process
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_pipeline(cmd: list[str], workdir: str, pre_lines: list[str] | None = None):
    global _run_process, _node_states

    with _run_lock:
        if _run_process and _run_process.poll() is None:
            _broadcast({'type': 'error', 'data': 'A run is already in progress.'})
            return

    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    # Mark the run as GUI-owned: kpl then writes source='gui' into
    # cache/run_state.json, so _external_running() ignores it.
    env['KPL_GUI_RUN'] = '1'
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        cwd=workdir,
        env=env,
        start_new_session=True,  # own process group so killpg kills the full tree
    )

    with _run_lock:
        _run_process = proc

    _node_states.clear()
    _broadcast({'type': 'run_start'})
    # Emitted after run_start because the GUI clears its log panel on run_start.
    for line in (pre_lines or []):
        _broadcast({'type': 'log', 'data': line})

    try:
        for line in proc.stdout:
            line = line.rstrip()

            kps_m = _KPS_RE.search(line)
            kpm_m = _KPM_RE.search(line)
            kpl_m = _KPL_RE.search(line)

            if kps_m:
                try:
                    data = json.loads(kps_m.group(1))
                    node_name = data.get('node')
                    state = data.get('state')
                    if node_name and state:
                        _node_states[node_name] = state
                        _broadcast({'type': 'state', 'node': node_name, 'state': state})
                except Exception:
                    pass
            elif kpm_m:
                try:
                    data = json.loads(kpm_m.group(1))
                    _broadcast({'type': 'progress', **data})
                except Exception:
                    pass
            elif kpl_m:
                _broadcast({'type': 'log', 'data': kpl_m.group(1)})
            else:
                _broadcast({'type': 'log', 'data': line})

    except Exception as e:
        _broadcast({'type': 'error', 'data': str(e)})
    finally:
        rc = proc.wait()
        success = rc == 0
        if not success:
            for node_name, state in list(_node_states.items()):
                if state in ('RUNNING', 'PENDING'):
                    _node_states[node_name] = 'FAILED'
                    _broadcast({'type': 'state', 'node': node_name, 'state': 'FAILED'})
        _broadcast({'type': 'run_end', 'success': success, 'return_code': rc})
        with _run_lock:
            _run_process = None


def _clean_then_run(clean_cmd: list[str] | None, run_cmd: list[str], workdir: str,
                    note: str | None = None):
    """Drop the selected node's cache, then run it. Upstream nodes keep their
    caches and are reused (caching is the default); only the node the user
    explicitly asked to run is forced to re-execute."""
    with _run_lock:
        if _run_process and _run_process.poll() is None:
            _broadcast({'type': 'error', 'data': 'A run is already in progress.'})
            return

    pre_lines = [note] if note else []
    if clean_cmd:
        try:
            res = subprocess.run(clean_cmd, cwd=workdir, capture_output=True,
                                 text=True, timeout=120)
            pre_lines += [l for l in (res.stdout or '').splitlines() if l.strip()]
        except Exception as exc:
            pre_lines.append(f'[warning] Cache clean failed: {exc}')

    _run_pipeline(run_cmd, workdir, pre_lines=pre_lines)


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


def _graph_config_path() -> str:
    return os.path.join(_workdir, 'graph_config.yaml')


def _cache_root() -> str:
    """Resolved cache dir for _workdir (graph_config.yaml `cache_dir`, default 'cache')."""
    return read_configured_dirs(_workdir)[0]


def _data_root() -> str:
    """Resolved data dir for _workdir (graph_config.yaml `data_dir`, default 'data')."""
    return read_configured_dirs(_workdir)[1]


# ── GUI config (config.yaml in the kpipeliner root dir) ────────────────────

_DEFAULT_GUI_CONFIG = {
    'background': {'pattern': 'dots', 'gap': 20, 'size': 1, 'color': '#2d3748'},
    'snap': {'enabled': True},
}


def _gui_config_path() -> str:
    return config_path()


def _load_gui_config() -> dict:
    cfg = {}
    if os.path.exists(_gui_config_path()):
        try:
            with open(_gui_config_path()) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    gui = cfg.get('gui', {}) or {}
    return {
        section: {**defaults, **(gui.get(section) or {})}
        for section, defaults in _DEFAULT_GUI_CONFIG.items()
    }


@app.get('/api/config')
def get_config():
    return JSONResponse(_load_gui_config())


@app.post('/api/config')
async def save_config(request_data: dict):
    """Merge posted sections (e.g. {'background': {'gap': 25}}) into config.yaml."""
    path = _gui_config_path()
    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    gui = cfg.get('gui', {}) or {}
    for section, values in request_data.items():
        if section in _DEFAULT_GUI_CONFIG and isinstance(values, dict):
            gui[section] = {**(gui.get(section) or {}), **values}
    cfg['gui'] = gui
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)
    return JSONResponse({'ok': True, **_load_gui_config()})


def _pipeline_groups_setting():
    """The pipeline's `groups_dir` value as written in graph_config.yaml (None if unset)."""
    try:
        with open(_graph_config_path()) as f:
            return (yaml.safe_load(f) or {}).get('groups_dir') or None
    except Exception:
        return None


def _group_read_dirs() -> list:
    """Directories group components are read from: the pipeline's `groups_dir` first,
    then the installation-wide group_base_nodes dir (older exports)."""
    return resolve_groups_dirs(config_dir(), _workdir, _pipeline_groups_setting())


def _group_save_dir() -> str:
    """Where an exported group is written: the pipeline's `groups_dir` (graph_config.yaml);
    a pipeline that has not set one keeps the installation-wide group_base_nodes dir."""
    return resolve_groups_dir(_workdir, _pipeline_groups_setting()) or get_group_base_nodes_dir(config_dir())


def _list_group_components() -> list[dict]:
    """Exported group components from the pipeline's groups_dir and the installation's
    group_base_nodes dir (first one wins on a name clash), shaped like node-type
    entries so the components panel can list and drag them. They are listed
    after the base nodes, under 'groups/<category>'."""
    comps = []
    seen = set()

    def read(cat, fname, path):
        if not fname.endswith('.yaml'):
            return
        name = os.path.splitext(fname)[0]
        if (cat, name) in seen:      # an earlier dir (the pipeline's groups_dir) has it
            return
        try:
            cfg = yaml.safe_load(open(path)) or {}
        except Exception:
            return
        seen.add((cat, name))
        comps.append({
            'type': f'{cat}/{name}' if cat else name,
            'description': cfg.get('description', ''),
            'category': f'groups/{cat}' if cat else 'groups',
            'inputs': cfg.get('inputs') or [],
            'input_types': {},
            'outputs': cfg.get('outputs') or [],
            'default': {},
            'caption': '',
            'is_group': True,
            'group_category': cat,
            'group_name': name,
        })

    for gdir in _group_read_dirs():
        if not os.path.isdir(gdir):
            continue
        for entry in sorted(os.listdir(gdir)):
            if entry.startswith('_') or entry.startswith('.'):
                continue
            p = os.path.join(gdir, entry)
            if os.path.isdir(p):
                for f in sorted(os.listdir(p)):
                    read(entry, f, os.path.join(p, f))
            else:
                read('', entry, p)
    return comps


@app.get('/api/nodes')
def get_nodes():
    graph = KGraph(verbose=False)
    os.chdir(_workdir)
    graph.load_externals()
    if os.path.exists(_graph_config_path()):
        try:
            graph.load_from_yaml(_graph_config_path())
        except Exception:
            pass
    return JSONResponse(graph.list_available_node_types() + _list_group_components())


@app.get('/api/graph')
def get_graph():
    cfg_path = _graph_config_path()
    if not os.path.exists(cfg_path):
        return JSONResponse({'nodes': [], 'edges': [], 'missing': []})
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}

    nodes = []
    edges = []
    missing = []
    installed = scan_base_nodes(resolve_base_nodes_dirs(
        config_dir(),
        load_default=bool(cfg.get('load_default_base_nodes_dir', True)),
        custom_dirs=cfg.get('custom_base_nodes_dirs') or [],
        pipeline_dir=_workdir,
    ))
    ref_re = re.compile(r'^nodes\.(\w+)\.(\w+)$')

    # Per-link GUI metadata (handle_color / hide), keyed by the connection's
    # own endpoints — edges have no stable id across reloads, so the
    # (source, sourceHandle, target, targetHandle) tuple IS the key.
    edge_meta = {}
    for m in (cfg.get('edge_meta', []) or []):
        key = (m.get('source'), m.get('sourceHandle'), m.get('target'), m.get('targetHandle'))
        meta = {}
        if m.get('handle_color'):
            meta['handle_color'] = m['handle_color']
        if m.get('hide'):
            meta['hide'] = True
        if meta:
            edge_meta[key] = meta

    def _edge_data(source, source_handle, target, target_handle):
        return dict(edge_meta.get((source, source_handle, target, target_handle)) or {})

    # Pipeline input/output node markers (metadata; GUI colors title bars).
    input_nodes = set(cfg.get('inputs') or [])
    output_nodes = set(cfg.get('outputs') or [])
    # Frozen nodes keep their cache through clear-cache/clean operations
    # (kgraph.KGraph.frozen_nodes) — GUI shows a lock badge for these.
    frozen_nodes = set(cfg.get('frozen_nodes') or [])

    # Group ports: validate group_links against the declared ports, and note
    # which node inputs are fed through a port — their direct `nodes.X.Y` ref
    # edge must be suppressed or the routed path would show up twice.
    grp_ports = {
        g['id']: {'in': set(g.get('inputs') or []), 'out': set(g.get('outputs') or [])}
        for g in cfg.get('groups', []) or []
    }
    # For resolving a node's bound group slots (underlying_groups) to a
    # current label/existence check below.
    groups_by_id = {g['id']: g for g in cfg.get('groups', []) or []}
    # Every group id bound to at least one node's underlying_groups slot -
    # these default to `disabled` unless something has explicitly set
    # `disabled` on the group itself (a previous enable-click, or a hand
    # edit) - see the group-loading loop further down. Filled in by the
    # node loop below.
    referenced_group_ids = set()

    def _port_ok(nid, handle):
        if nid not in grp_ports:
            return True  # plain node endpoint
        if not handle:
            return False
        if handle in ('jump-in', 'jump-out'):
            return True  # groups always have exactly one, never declared
        if handle.startswith('gin-'):
            return handle[4:] in grp_ports[nid]['in']
        if handle.startswith('gout-'):
            return handle[5:] in grp_ports[nid]['out']
        return False

    group_links = [
        l for l in (cfg.get('group_links', []) or [])
        if _port_ok(l.get('source'), l.get('sourceHandle'))
        and _port_ok(l.get('target'), l.get('targetHandle'))
    ]
    covered = {
        (l['target'], l['targetHandle'])
        for l in group_links if l.get('target') not in grp_ports
    }

    # Jump links: execution-order-only, no data. Entries resolved through a
    # group boundary (`via_group`, written by save_graph) are dropped here —
    # they're the flattened, KGraph-facing form of hops that are ALSO stored
    # as group_links (below), which already draws them as the visible
    # multi-hop chain. Materializing both would duplicate the line.
    jump_links = [
        l for l in (cfg.get('jump_links', []) or [])
        if l.get('source') and l.get('target') and not l.get('via_group')
    ]

    # A node TYPE's node_config.yaml is identical for every INSTANCE of that
    # type — re-parsing it once per node (as this loop used to) makes a
    # large graph's load time scale with node *count*, not node *type*
    # count, and yaml.safe_load is expensive enough (pure-Python parser)
    # that this dominates get_graph's response time on a several-hundred-node
    # graph (measured: ~2s of a ~2s call). Cache per type for this request.
    node_type_cfg_cache = {}

    def _node_type_cfg(ntype):
        if ntype in node_type_cfg_cache:
            return node_type_cfg_cache[ntype]
        base_cfg_path = os.path.join(installed[ntype]['dir'], 'node_config.yaml')
        inp_names, out_names, is_kwui, group_slots = [], [], False, []
        node_slots = []
        if os.path.exists(base_cfg_path):
            try:
                bc = yaml.safe_load(open(base_cfg_path)) or {}
                inp_names = [i['name'] for i in bc.get('inputs', []) or []]
                out_names = [o['name'] for o in bc.get('outputs', []) or []]
                is_kwui = bool(bc.get('kwui', False))
                # Same declaration shape as inputs/outputs above ({name: ...}
                # dicts), but tolerate a bare string too (accepted since this
                # was introduced, and cheap to keep accepting). `description`
                # is carried through so the Properties panel can show it next
                # to the slot; `address` (a group component's category/name)
                # is only needed client-side at node-creation time, so it's
                # not resolved per-instance here.
                for g in (bc.get('underlying_groups', []) or []):
                    gname = g.get('name') if isinstance(g, dict) else g
                    if not gname:
                        continue
                    group_slots.append({
                        'name': gname,
                        'description': (g.get('description', '') if isinstance(g, dict) else ''),
                    })
                # Underlying-NODE slots: same declaration shape/tolerance as
                # underlying_groups above; `type` (optional) = the node type
                # the slot expects.
                for n in (bc.get('underlying_nodes', []) or []):
                    nname = n.get('name') if isinstance(n, dict) else n
                    if not nname:
                        continue
                    node_slots.append({
                        'name': nname,
                        'description': (n.get('description', '') if isinstance(n, dict) else ''),
                        'type': (n.get('type') if isinstance(n, dict) else None),
                    })
            except Exception:
                pass
        result = (inp_names, out_names, is_kwui, group_slots, node_slots)
        node_type_cfg_cache[ntype] = result
        return result

    all_node_names = {n.get('name') for n in cfg.get('nodes', []) or []}
    for node_cfg in cfg.get('nodes', []) or []:
        nid = node_cfg['name']
        ntype = node_cfg.get('type', '')
        pos = node_cfg.get('gui', {}).get('position', [0, 0]) or [0, 0]
        color = node_cfg.get('gui', {}).get('color')
        group = node_cfg.get('gui', {}).get('group')

        # Node types not installed in the base-nodes dir are reported so the
        # GUI can ask the user whether to load the graph without them.
        if ntype not in installed:
            missing.append({'name': nid, 'type': ntype})
            continue

        inp_names, out_names, is_kwui, group_slots, node_slots = _node_type_cfg(ntype)
        input_values = {}

        for inp in node_cfg.get('inputs', []) or []:
            if 'value' in inp:
                input_values[inp['name']] = inp['value']

        # Per-instance group-slot bindings (this node's own `groups:` list -
        # same shape/round-trip as `inputs:`/`outputs:` above), one entry per
        # slot the TYPE declares, bound or not. group_id/label are None for
        # an unbound slot, or a bound id that no longer matches any group in
        # this pipeline (e.g. deleted since) - the Properties panel's
        # dropdown and the under-node chip list both key off this.
        bound_by_name = {}
        for grp in node_cfg.get('groups', []) or []:
            if grp.get('name'):
                bound_by_name[grp['name']] = grp.get('value')
        underlying_groups = []
        for slot in group_slots:
            bound_id = bound_by_name.get(slot['name'])
            target = groups_by_id.get(bound_id) if bound_id else None
            underlying_groups.append({
                'name': slot['name'],
                'description': slot.get('description', ''),
                'group_id': target['id'] if target else None,
                'label': target.get('label', target['id']) if target else None,
            })
            if target:
                referenced_group_ids.add(target['id'])

        # Per-instance underlying-NODE slot bindings (this node's own
        # `underlying_nodes:` list), one entry per slot the TYPE declares,
        # bound or not — same shape/round-trip as underlying_groups above.
        # node_id is None for an unbound slot, or a bound name that no longer
        # matches any node in this pipeline (deleted/renamed since).
        bound_nodes = {}
        for un in node_cfg.get('underlying_nodes', []) or []:
            if un.get('name'):
                bound_nodes[un['name']] = un.get('value')
        underlying_nodes = []
        for slot in node_slots:
            bound_nid = bound_nodes.get(slot['name'])
            underlying_nodes.append({
                'name': slot['name'],
                'description': slot.get('description', ''),
                'type': slot.get('type'),
                'node_id': bound_nid if bound_nid in all_node_names else None,
            })

        rf_node = {
            'id': nid,
            'type': 'ioNode',
            'position': {'x': pos[0], 'y': pos[1]},
            'data': {
                'label': nid,
                'node_type': ntype,
                'inputs': inp_names,
                'outputs': out_names,
                'input_values': input_values,
                'color': color,
                'state': 'PENDING',
                'is_input': nid in input_nodes,
                'is_output': nid in output_nodes,
                'frozen': nid in frozen_nodes,
                # Derived from the node TYPE's node_config.yaml, never persisted
                # back into graph_config.yaml (save_graph reads an explicit key
                # list) — purely so the GUI can mark kwui nodes visually.
                'is_kwui': is_kwui,
                # Declared slots (names, from node_config.yaml) merged with
                # this instance's bindings (values, from graph_config.yaml) -
                # the binding part DOES round-trip on save (see save_graph),
                # unlike is_kwui above.
                'underlying_groups': underlying_groups,
                'underlying_nodes': underlying_nodes,
            },
        }
        if group:
            rf_node['parentNode'] = group
            rf_node['extent'] = 'parent'

        nodes.append(rf_node)

        for inp in node_cfg.get('inputs', []) or []:
            val = inp.get('value')
            if (nid, inp['name']) in covered:
                continue  # input is routed through a group port — link edge below
            if isinstance(val, str):
                m = ref_re.match(val)
                if m:
                    edges.append({
                        'id': f'e_{m.group(1)}_{nid}_{m.group(2)}_{inp["name"]}',
                        'source': m.group(1),
                        'target': nid,
                        'sourceHandle': m.group(2),
                        'targetHandle': inp['name'],
                        'data': _edge_data(m.group(1), m.group(2), nid, inp['name']),
                    })

    # Load group nodes
    for grp in cfg.get('groups', []) or []:
        gid = grp['id']
        pos = grp.get('position', [0, 0])
        nodes.append({
            'id': gid,
            'type': 'groupNode',
            'position': {'x': pos[0], 'y': pos[1]},
            'style': {
                'width': grp.get('width', 400),
                'height': grp.get('height', 300),
            },
            'data': {
                'label': grp.get('label', gid),
                'color': grp.get('color', '#4a5568'),
                'folded': grp.get('folded', False),
                'description': grp.get('description', ''),
                'group_inputs': grp.get('inputs') or [],
                'group_outputs': grp.get('outputs') or [],
                # hidden/disabled/highlight_color: no DIRECT Properties-panel
                # control on the group itself (still set by hand-editing
                # graph_config.yaml, or indirectly via a node's underlying_groups
                # chip list - see the node loop above). Loaded/saved like any
                # other group field so a flag set this way survives a GUI save.
                'hidden': grp.get('hidden', False),
                # A group bound as some node's underlying_groups slot defaults
                # to disabled until explicitly turned on (a chip click, or a
                # hand edit) - distinguish "key absent" from "explicitly
                # false" so that explicit choice always wins once made.
                'disabled': (
                    bool(grp.get('disabled')) if grp.get('disabled') is not None
                    else gid in referenced_group_ids
                ),
                'highlight_color': grp.get('highlight_color'),
            },
        })

    for l in group_links:
        edges.append({
            'id': f"gl_{l.get('source')}_{l.get('target')}_{l.get('sourceHandle')}_{l.get('targetHandle')}",
            'source': l.get('source'),
            'sourceHandle': l.get('sourceHandle'),
            'target': l.get('target'),
            'targetHandle': l.get('targetHandle'),
            'data': _edge_data(l.get('source'), l.get('sourceHandle'), l.get('target'), l.get('targetHandle')),
        })

    for l in jump_links:
        edges.append({
            'id': f"jl_{l.get('source')}_{l.get('target')}",
            'source': l.get('source'),
            'sourceHandle': 'jump-out',
            'target': l.get('target'),
            'targetHandle': 'jump-in',
            'data': _edge_data(l.get('source'), 'jump-out', l.get('target'), 'jump-in'),
        })

    # Drop edges that reference a skipped (missing-type) node.
    node_ids = {n['id'] for n in nodes}
    edges = [e for e in edges if e['source'] in node_ids and e['target'] in node_ids]

    return JSONResponse({'nodes': nodes, 'edges': edges, 'missing': missing, 'meta': {
        'name': cfg.get('name', ''),
        'description': cfg.get('description', ''),
    }})


@app.post('/api/graph')
async def save_graph(request_data: dict):
    """
    Receives { nodes, edges } from React and converts back to graph_config.yaml.
    """
    rf_nodes = request_data.get('nodes', [])
    rf_edges = request_data.get('edges', [])

    # Per-link GUI metadata (handle_color / hide, set from the Properties
    # panel when a link is selected) — captured from the raw incoming edges
    # before any filtering below, keyed by endpoints (see get_graph's
    # `edge_meta`, the same identity used to read it back on load).
    edge_meta_out = []
    for e in rf_edges:
        d = e.get('data') or {}
        hc = d.get('handle_color')
        hide = bool(d.get('hide'))
        if not hc and not hide:
            continue
        entry = {
            'source': e.get('source'), 'sourceHandle': e.get('sourceHandle'),
            'target': e.get('target'), 'targetHandle': e.get('targetHandle'),
        }
        if hc:
            entry['handle_color'] = hc
        if hide:
            entry['hide'] = True
        edge_meta_out.append(entry)

    group_ids = {n['id'] for n in rf_nodes if n.get('type') == 'groupNode'}

    # Jump edges (execution-order-only, no data) are pulled out up front so
    # they never reach the group-port / node-input resolution below — they
    # must never be written as a `nodes.X.Y` value reference. A group acts as
    # a jump router exactly like it does for data ports: its own `jump-in`/
    # `jump-out` handle plays two roles sharing that id (outer + inner, see
    # GroupNode.jsx) — so a group-boundary jump link is always TWO edges
    # (outside <-> group, group <-> child), never one. Both are kept as
    # `group_links` (so the GUI redraws the same two hops on reload) AND
    # resolved down to a flat, real-node `jump_links` pair (tagged
    # `via_group`) so KGraph — which has no concept of groups — still sees a
    # plain node-to-node dependency.
    def _is_jump_handle(h):
        return h in ('jump-out', 'jump-in')

    jump_entry_feed = {}   # group_id -> child_id (single value, group's inner jump-in -> child)
    jump_exit_feed = {}    # group_id -> [child_id, ...] (fan-in, child jump-out -> group's inner jump-out)
    boundary_jump_edges = []  # (source, target) still needing group resolution
    plain_jump_pairs = []     # (source, target), both endpoints already real nodes
    group_jump_links = []     # GUI-only redraw entries, merged into group_links below

    for e in rf_edges:
        sh, th = e.get('sourceHandle'), e.get('targetHandle')
        if not (_is_jump_handle(sh) and _is_jump_handle(th)):
            continue
        s, t = e.get('source'), e.get('target')
        s_grp, t_grp = s in group_ids, t in group_ids
        if not s_grp and not t_grp:
            plain_jump_pairs.append((s, t))
        elif sh == 'jump-in' and th == 'jump-in' and s_grp and not t_grp:
            jump_entry_feed[s] = t  # inner: group's jump-in -> a child
            group_jump_links.append({'source': s, 'sourceHandle': sh, 'target': t, 'targetHandle': th})
        elif sh == 'jump-out' and th == 'jump-out' and t_grp and not s_grp:
            jump_exit_feed.setdefault(t, []).append(s)  # inner: a child -> group's jump-out
            group_jump_links.append({'source': s, 'sourceHandle': sh, 'target': t, 'targetHandle': th})
        else:
            boundary_jump_edges.append((s, t))  # outer hop: external<->group or group<->group
            group_jump_links.append({'source': s, 'sourceHandle': sh, 'target': t, 'targetHandle': th})
    rf_edges = [e for e in rf_edges
                if not (_is_jump_handle(e.get('sourceHandle')) and _is_jump_handle(e.get('targetHandle')))]

    def _resolve_jump_entry(gid, seen=frozenset()):
        """Follow a group's inner jump-in feed to a single real child."""
        if gid in seen:
            return None  # routing cycle
        child = jump_entry_feed.get(gid)
        if child is None:
            return None  # port not wired to anything inside yet
        return _resolve_jump_entry(child, seen | {gid}) if child in group_ids else child

    def _resolve_jump_exits(gid, seen=frozenset()):
        """Follow a group's inner jump-out feed(s) to real children, flattening nested groups."""
        if gid in seen:
            return []
        out = []
        for child in jump_exit_feed.get(gid, []):
            out.extend(_resolve_jump_exits(child, seen | {gid}) if child in group_ids else [child])
        return out

    jump_pairs = list(plain_jump_pairs)
    via_group_pairs = set()
    for s, t in boundary_jump_edges:
        sources = _resolve_jump_exits(s) if s in group_ids else [s]
        target = _resolve_jump_entry(t) if t in group_ids else t
        if target is None:
            continue
        for rs in sources:
            via_group_pairs.add((rs, target))
    jump_pairs.extend(via_group_pairs)

    # A node's resolved jump-out feeds at most one target — keep the first if
    # more than one pair shares a source (shouldn't happen; GraphPanel's
    # onConnect already replaces rather than adds, for both plain and
    # group-inner single-target handles).
    jump_links = []
    seen_jump_sources = set()
    for s, t in jump_pairs:
        if s in seen_jump_sources:
            continue
        seen_jump_sources.add(s)
        entry = {'source': s, 'target': t}
        if (s, t) in via_group_pairs:
            entry['via_group'] = True
        jump_links.append(entry)

    def _is_port(nid, handle):
        return nid in group_ids and isinstance(handle, str) \
            and (handle.startswith('gin-') or handle.startswith('gout-'))

    # Edges touching a group port are GUI routing, persisted as group_links.
    # Any other edge endpoint on a group (e.g. the synthetic folded ext-*
    # handles) is not a real connection — drop those edges entirely.
    group_links = []
    port_feed = {}  # (group_id, port_handle) -> (source, sourceHandle)
    plain_edges = []
    for e in rf_edges:
        src_grp = e['source'] in group_ids
        tgt_grp = e['target'] in group_ids
        if not src_grp and not tgt_grp:
            plain_edges.append(e)
            continue
        if (src_grp and not _is_port(e['source'], e.get('sourceHandle'))) or \
           (tgt_grp and not _is_port(e['target'], e.get('targetHandle'))):
            continue
        group_links.append({
            'source': e['source'], 'sourceHandle': e.get('sourceHandle'),
            'target': e['target'], 'targetHandle': e.get('targetHandle'),
        })
        if tgt_grp:
            port_feed[(e['target'], e['targetHandle'])] = (e['source'], e.get('sourceHandle'))
        else:
            plain_edges.append(e)  # group port -> node input, resolved below
    group_links.extend(group_jump_links)

    def _resolve(src, handle):
        """Follow group-port routing back to a concrete node output."""
        seen = set()
        while src in group_ids:
            key = (src, handle)
            if key in seen:
                return None  # routing cycle
            seen.add(key)
            nxt = port_feed.get(key)
            if nxt is None:
                return None  # port not fed from anywhere
            src, handle = nxt
        return src, handle

    # (target, input) -> (source, sourceHandle) resolved through ports, or
    # None when the input is port-connected but the chain is not fed yet.
    edge_map = {}
    for e in plain_edges:
        edge_map[(e['target'], e['targetHandle'])] = _resolve(e['source'], e.get('sourceHandle'))

    cfg_path = _graph_config_path()
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {'name': 'graph', 'description': '', 'author': '', 'date': '', 'version': '1.0'}

    cfg_nodes = []
    cfg_groups = []

    for n in rf_nodes:
        nid = n['id']
        ntype = n.get('data', {}).get('node_type', n.get('type', ''))
        pos = n.get('position', {'x': 0, 'y': 0})
        color = n.get('data', {}).get('color')
        parent = n.get('parentNode')

        if n.get('type') == 'groupNode':
            cfg_groups.append({
                'id': nid,
                'label': n['data'].get('label', nid),
                'position': [pos.get('x', 0), pos.get('y', 0)],
                'width': n.get('style', {}).get('width', 400),
                'height': n.get('style', {}).get('height', 300),
                'color': n['data'].get('color', '#4a5568'),
                'folded': n['data'].get('folded', False),
                'description': n['data'].get('description', ''),
                'inputs': n['data'].get('group_inputs') or [],
                'outputs': n['data'].get('group_outputs') or [],
                # No Properties-panel control sets these yet (hand-edit
                # graph_config.yaml or a script) - round-tripped here so a
                # flag set that way survives an unrelated GUI save instead of
                # being silently dropped.
                'hidden': n['data'].get('hidden', False),
                'disabled': n['data'].get('disabled', False),
                'highlight_color': n['data'].get('highlight_color'),
            })
            continue

        inp_names = n.get('data', {}).get('inputs', [])
        out_names = n.get('data', {}).get('outputs', [])
        input_values = n.get('data', {}).get('input_values', {})

        inputs = []
        for inp in inp_names:
            key = (nid, inp)
            if key in edge_map:
                resolved = edge_map[key]
                if resolved is not None:
                    inputs.append({'name': inp, 'value': f'nodes.{resolved[0]}.{resolved[1]}'})
                else:
                    # Connected to a group port whose chain is not fed yet —
                    # store no value (a stale input_values ref must not win).
                    inputs.append({'name': inp})
            elif inp in input_values and input_values[inp] is not None:
                inputs.append({'name': inp, 'value': input_values[inp]})
            else:
                inputs.append({'name': inp})

        node_entry = {
            'name': nid,
            'type': ntype,
            'gui': {
                'position': [pos.get('x', 0), pos.get('y', 0)],
                'color': color,
            },
            'inputs': inputs,
            'outputs': [{'name': o} for o in out_names],
            # Per-instance group-slot bindings (see the node loop in
            # get_graph) - this DOES round-trip, unlike is_kwui/inputs/
            # outputs' name lists, since a slot's bound value is this
            # instance's own state, settable via the Properties panel.
            'groups': [
                {'name': s['name'], 'value': s.get('group_id')}
                for s in n.get('data', {}).get('underlying_groups', []) or []
            ],
        }
        # Underlying-NODE slot bindings round-trip like `groups` above, but the
        # key is only written for nodes that actually have slots — so a
        # pipeline without any stays byte-identical on save.
        un_slots = n.get('data', {}).get('underlying_nodes', []) or []
        if un_slots:
            node_entry['underlying_nodes'] = [
                {'name': s['name'], 'value': s.get('node_id')}
                for s in un_slots
            ]
        if parent:
            node_entry['gui']['group'] = parent

        cfg_nodes.append(node_entry)

    cfg['nodes'] = cfg_nodes
    # Pipeline input/output markers live as is_input/is_output flags on the
    # GUI nodes (they survive renames/deletes for free) and are persisted as
    # the top-level `inputs`/`outputs` name lists.
    cfg['inputs'] = [n['id'] for n in rf_nodes
                     if n.get('type') != 'groupNode' and n.get('data', {}).get('is_input')]
    cfg['outputs'] = [n['id'] for n in rf_nodes
                      if n.get('type') != 'groupNode' and n.get('data', {}).get('is_output')]
    # Frozen markers live as a `frozen` flag on the GUI node (same pattern as
    # is_input/is_output) and are persisted as the top-level `frozen_nodes`
    # list that kgraph.KGraph / kpipeline.py clean logic already reads.
    cfg['frozen_nodes'] = [n['id'] for n in rf_nodes
                           if n.get('type') != 'groupNode' and n.get('data', {}).get('frozen')]
    if cfg_groups:
        cfg['groups'] = cfg_groups
    elif 'groups' in cfg:
        del cfg['groups']
    if group_links:
        cfg['group_links'] = group_links
    elif 'group_links' in cfg:
        del cfg['group_links']
    if jump_links:
        cfg['jump_links'] = jump_links
    elif 'jump_links' in cfg:
        del cfg['jump_links']
    if edge_meta_out:
        cfg['edge_meta'] = edge_meta_out
    elif 'edge_meta' in cfg:
        del cfg['edge_meta']

    with open(cfg_path, 'w') as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    return JSONResponse({'ok': True})


# ── Group components (export a group as a reusable template) ────────────────


@app.post('/api/export-group')
async def export_group(request_data: dict):
    """
    Write a group (its meta, ports, child nodes with internal connections and
    port routing) as a reusable component into the pipeline's `groups_dir`
    (graph_config.yaml; without one: the installation's group_base_nodes dir):
    <groups_dir>/<category>/<name>.yaml. External connections are
    main-graph specific and are dropped; positions are already group-relative.
    """
    gid = request_data.get('group_id')
    name = re.sub(r'\W', '_', str(request_data.get('name') or '').strip())
    category = re.sub(r'\W', '_', str(request_data.get('category') or '').strip())
    overwrite = bool(request_data.get('overwrite'))
    rf_nodes = request_data.get('nodes', [])
    rf_edges = request_data.get('edges', [])

    if not name:
        return JSONResponse({'ok': False, 'msg': 'Component name required.'}, status_code=400)
    group = next((n for n in rf_nodes if n.get('id') == gid and n.get('type') == 'groupNode'), None)
    if group is None:
        return JSONResponse({'ok': False, 'msg': 'Group not found.'}, status_code=404)

    children = [n for n in rf_nodes if n.get('parentNode') == gid]
    internal = {n['id'] for n in children}

    # Internal node→node edges become refs; port edges become group_links
    # with '__group__' standing in for the group itself. Jump edges (no data,
    # execution-order-only) are classified first and separately — an
    # internal-to-internal jump edge is NOT a data feed (unlike a normal
    # node→node edge) and must not fall into the `feeds` branch below, or it
    # silently vanishes (feeds keys that don't match a declared input name
    # are simply never read back out).
    feeds = {}  # (child_id, input) -> 'nodes.X.Y' from an internal edge
    links = []
    tpl_jump_links = []  # {source, target} between two internal children
    for e in rf_edges:
        s, t = e.get('source'), e.get('target')
        sh, th = e.get('sourceHandle'), e.get('targetHandle')
        if sh in ('jump-out', 'jump-in') and th in ('jump-out', 'jump-in'):
            if s in internal and t in internal:
                tpl_jump_links.append({'source': s, 'target': t})
            elif s == gid and sh == 'jump-in' and t in internal:
                links.append({'source': '__group__', 'sourceHandle': sh,
                              'target': t, 'targetHandle': th})
            elif t == gid and th == 'jump-out' and s in internal:
                links.append({'source': s, 'sourceHandle': sh,
                              'target': '__group__', 'targetHandle': th})
            # else: touches the group's OUTER jump role (external <-> group)
            # — main-graph specific, dropped like any other external ref.
            continue
        if t in internal:
            if s in internal:
                feeds[(t, th)] = f'nodes.{s}.{sh}'
            elif s == gid and isinstance(sh, str) and sh.startswith('gin-'):
                links.append({'source': '__group__', 'sourceHandle': sh,
                              'target': t, 'targetHandle': th})
        elif t == gid and isinstance(th, str) and th.startswith('gout-'):
            if s in internal:
                links.append({'source': s, 'sourceHandle': sh,
                              'target': '__group__', 'targetHandle': th})
            elif s == gid and isinstance(sh, str) and sh.startswith('gin-'):
                links.append({'source': '__group__', 'sourceHandle': sh,
                              'target': '__group__', 'targetHandle': th})

    ref_re = re.compile(r'^nodes\.(\w+)\.(\w+)$')
    tpl_nodes = []
    for n in children:
        data = n.get('data', {})
        input_values = data.get('input_values', {}) or {}
        inputs = []
        for inp in data.get('inputs', []) or []:
            if (n['id'], inp) in feeds:
                inputs.append({'name': inp, 'value': feeds[(n['id'], inp)]})
                continue
            val = input_values.get(inp)
            if isinstance(val, str):
                m = ref_re.match(val)
                if m and m.group(1) not in internal:
                    val = None  # ref to a node outside the group
            if val is not None:
                inputs.append({'name': inp, 'value': val})
            else:
                inputs.append({'name': inp})
        pos = n.get('position', {})
        tpl_nodes.append({
            'name': n['id'],
            'type': data.get('node_type', ''),
            'position': [pos.get('x', 0), pos.get('y', 0)],
            'color': data.get('color'),
            'inputs': inputs,
        })

    gdata = group.get('data', {})
    tpl = {
        'name': name,
        'description': gdata.get('description', ''),
        'label': gdata.get('label', name),
        'color': gdata.get('color', '#2d4a6b'),
        'width': group.get('style', {}).get('width', 360),
        'height': group.get('style', {}).get('height', 280),
        'inputs': gdata.get('group_inputs') or [],
        'outputs': gdata.get('group_outputs') or [],
        'nodes': tpl_nodes,
        'group_links': links,
        'jump_links': tpl_jump_links,
    }

    gdir = _group_save_dir()
    target_dir = os.path.join(gdir, category) if category else gdir
    path = os.path.join(target_dir, f'{name}.yaml')
    if os.path.exists(path) and not overwrite:
        shown = f'{category}/{name}' if category else name
        return JSONResponse({'ok': False, 'exists': True,
                             'msg': f"Component '{shown}' already exists."}, status_code=409)
    os.makedirs(target_dir, exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(tpl, f, sort_keys=False, default_flow_style=False)
    return JSONResponse({'ok': True, 'path': path})


@app.get('/api/group-component')
def get_group_component(category: str = '', name: str = ''):
    """Return a group component template (for expansion on drop)."""
    safe_cat = os.path.basename(category) if category else ''
    safe_name = os.path.basename(name)
    path = None
    for gdir in _group_read_dirs():     # the pipeline's groups_dir first
        parts = [gdir, safe_cat, f'{safe_name}.yaml'] if safe_cat else [gdir, f'{safe_name}.yaml']
        candidate = os.path.join(*parts)
        if os.path.isfile(candidate):
            path = candidate
            break
    if path is None:
        return JSONResponse({'ok': False, 'msg': 'Component not found.'}, status_code=404)
    try:
        tpl = yaml.safe_load(open(path)) or {}
    except Exception as exc:
        return JSONResponse({'ok': False, 'msg': str(exc)}, status_code=500)
    return JSONResponse(tpl)


def _kpl_cmd() -> list[str]:
    return kpl_command()


@app.post('/api/run-group')
async def run_group(request_data: dict):
    """Same as GroupRef.run() in node code: `kpl group run <id>` runs only what
    feeds the group's declared outputs (+ their upstream dependencies).

    A dedicated endpoint on purpose: a GUI newer than the running server must
    get a 404 here, not have an unknown `group_id` field silently ignored by
    /api/run — which would start a full pipeline run instead."""
    group_id = request_data.get('group_id')
    if not group_id:
        return JSONResponse({'ok': False, 'msg': 'group_id required'}, status_code=400)
    cmd = _kpl_cmd() + ['group', 'run', str(group_id)]
    threading.Thread(target=_run_pipeline, args=(cmd, _workdir), daemon=True).start()
    return JSONResponse({'ok': True})


@app.post('/api/run')
async def run_pipeline(request_data: dict):
    node_id = request_data.get('node_id')
    debug = request_data.get('debug', False)
    clear_cache = request_data.get('clear_cache', False)

    kpipeliner_cmd = kpl_command()

    sub = 'run_debug' if debug else 'run'

    if node_id:
        # Drop the selected node's cache first only when the user confirmed it
        # in the GUI dialog. Frozen nodes keep theirs — frozen means "preserve
        # my results".
        clean_cmd = None
        note = None
        if clear_cache:
            frozen = set()
            try:
                with open(_graph_config_path()) as f:
                    frozen = set((yaml.safe_load(f) or {}).get('frozen_nodes') or [])
            except Exception:
                pass
            if node_id in frozen:
                note = f"Node '{node_id}' is frozen — keeping its cache."
            else:
                clean_cmd = kpipeliner_cmd + ['clean', node_id]
        cmd = kpipeliner_cmd + [sub, node_id]
        threading.Thread(target=_clean_then_run, args=(clean_cmd, cmd, _workdir, note),
                         daemon=True).start()
    else:
        cmd = kpipeliner_cmd + [sub]
        threading.Thread(target=_run_pipeline, args=(cmd, _workdir), daemon=True).start()

    return JSONResponse({'ok': True})


# ---------------------------------------------------------------------------
# kwui node-UI designer (kwebui edit mode) — started on demand from the context menu
# ---------------------------------------------------------------------------

#: node type -> {'proc': Popen, 'port': int, 'tail': [last output lines]}
_design_servers: dict = {}
_design_lock = threading.Lock()
_DESIGN_FIRST_PORT = 8801   # clear of the GUI's 8601+ and a kwui run's 8701+


def _kwui_design_available() -> bool:
    """kwebui with edit mode (>= 1.0.0) importable here — the `kpl` the designer is started
    with runs in this same Python environment."""
    try:
        return importlib.util.find_spec('kwebui.editor') is not None
    except (ImportError, ValueError):
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def _stop_design_servers():
    for entry in list(_design_servers.values()):
        proc = entry['proc']
        if proc.poll() is None:
            proc.terminate()


atexit.register(_stop_design_servers)


@app.on_event('shutdown')
async def _stop_design_servers_on_shutdown():
    # atexit alone is not enough: on Ctrl+C / SIGTERM uvicorn re-raises the signal after its
    # graceful stop, so the interpreter ends without running atexit — the designers (and the
    # node each one may be running) would outlive the GUI server.
    _stop_design_servers()


def _start_design_server(node_type: str) -> dict:
    """`kpl kwui design <node_type>` as a background process (one per node type, reused
    while it runs). Returns {'ok', 'port'} or {'ok': False, 'error'}."""
    with _design_lock:
        entry = _design_servers.get(node_type)
        if entry and entry['proc'].poll() is None and _port_open(entry['port']):
            return {'ok': True, 'port': entry['port'], 'reused': True}
        port = _find_free_port(_bind_host, _DESIGN_FIRST_PORT)
        if port is None:
            return {'ok': False, 'error': 'No free port for the designer.'}
        cmd = kpl_command() + ['kwui', 'design', node_type, '--no-browser',
                               '--host', _bind_host, '--port', str(port)]
        proc = subprocess.Popen(cmd, cwd=_workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=dict(os.environ, PYTHONUNBUFFERED='1'))
        tail: list = []

        def pump():
            for line in proc.stdout:
                tail.append(line.rstrip())
                del tail[:-20]

        threading.Thread(target=pump, daemon=True).start()
        _design_servers[node_type] = {'proc': proc, 'port': port, 'tail': tail}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _port_open(port):
                return {'ok': True, 'port': port}
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        message = ' '.join(tail[-3:]) or 'The designer did not start.'
        if proc.poll() is None:
            proc.terminate()
        return {'ok': False, 'error': message}


@app.get('/api/kwui/design')
def kwui_design_available():
    return JSONResponse({'available': _kwui_design_available()})


@app.post('/api/kwui/design')
async def kwui_design(request_data: dict):
    node_type = str(request_data.get('node_type') or '')
    if not re.fullmatch(r'\w+', node_type):
        return JSONResponse({'ok': False, 'error': 'Invalid node type.'}, status_code=400)
    if not _kwui_design_available():
        return JSONResponse({'ok': False, 'error': 'The designer needs kwebui >= 1.0.0: '
                                                   'uv pip install -U "kpipeliner[kwui]"'}, status_code=400)
    result = await asyncio.to_thread(_start_design_server, node_type)
    return JSONResponse(result, status_code=200 if result.get('ok') else 500)


def _cache_uuid() -> str:
    """The pipeline uuid used in cache paths, read from extern/uuid.yaml the
    same way KGraph.load_externals does (fallback matches KGraph's default)."""
    path = os.path.join(_workdir, 'extern', 'uuid.yaml')
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return str(data.get('value', '123456789'))
    except Exception:
        return '123456789'


@app.get('/api/node-cache/{node_id}')
def node_cache_status(node_id: str):
    """Whether a node has cached data — the GUI only asks the clear-cache
    question when there is actually something to clear."""
    target = os.path.join(_cache_root(), _cache_uuid(), node_id)
    return JSONResponse({'cached': os.path.exists(target)})


@app.get('/api/node-outputs/{node_id}')
def node_outputs(node_id: str):
    """The node's cached outputs.yaml (cache/<uuid>/<node_id>/outputs.yaml), if any —
    lets the Properties panel show output values without a re-run."""
    safe = os.path.basename(node_id)  # no path traversal
    outputs_yaml = os.path.join(_cache_root(), _cache_uuid(), safe, 'outputs.yaml')
    if not os.path.exists(outputs_yaml):
        return JSONResponse({'cached': False, 'outputs': None})
    try:
        with open(outputs_yaml) as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        return JSONResponse({'cached': False, 'outputs': None, 'error': str(exc)})
    return JSONResponse({'cached': True, 'outputs': data})


@app.delete('/api/node-cache/{node_id}')
def remove_node_cache(node_id: str):
    """Remove a node's cache directory (cache/<uuid>/<node_id>)."""
    import shutil as _shutil

    safe = os.path.basename(node_id)  # no path traversal
    target = os.path.join(_cache_root(), _cache_uuid(), safe)
    if not os.path.isdir(target):
        return JSONResponse({'ok': False, 'msg': f"No cache for '{safe}'."}, status_code=404)
    try:
        _shutil.rmtree(target)
    except Exception as exc:
        return JSONResponse({'ok': False, 'msg': str(exc)}, status_code=500)
    return JSONResponse({'ok': True, 'msg': f"Cache for '{safe}' removed."})


@app.post('/api/remove-caches')
def remove_node_caches(body: dict):
    """Remove the cache dirs of several nodes at once (the GUI's group-level
    "Remove Caches" button). Same per-node effect as DELETE
    /api/node-cache/{id}; nodes with no cache are simply skipped."""
    import shutil as _shutil

    root = os.path.join(_cache_root(), _cache_uuid())
    removed, failed = [], []
    for node_id in body.get('node_ids', []) or []:
        target = os.path.join(root, os.path.basename(str(node_id)))
        if not os.path.isdir(target):
            continue
        try:
            _shutil.rmtree(target)
            removed.append(node_id)
        except Exception as exc:
            failed.append({'node': node_id, 'msg': str(exc)})
    return JSONResponse({'ok': not failed, 'removed': removed, 'failed': failed})


@app.get('/api/node-cache-zip/{node_id}')
def download_node_cache(node_id: str):
    """Zip a node's cache directory (cache/<uuid>/<node_id>) and download it."""
    import tempfile
    import zipfile
    from starlette.background import BackgroundTask

    safe = os.path.basename(node_id)  # no path traversal
    target = os.path.join(_cache_root(), _cache_uuid(), safe)
    if not os.path.isdir(target):
        return JSONResponse({'ok': False, 'msg': f"No cache for '{safe}'."}, status_code=404)

    fd, tmp_path = tempfile.mkstemp(prefix=f'{safe}_cache_', suffix='.zip')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(target):
                for fn in files:
                    full = os.path.join(root, fn)
                    zf.write(full, os.path.join(safe, os.path.relpath(full, target)))
    except Exception as exc:
        os.remove(tmp_path)
        return JSONResponse({'ok': False, 'msg': str(exc)}, status_code=500)

    return FileResponse(
        tmp_path,
        filename=f'{safe}_cache.zip',
        media_type='application/zip',
        background=BackgroundTask(os.remove, tmp_path),  # clean up after send
    )


# ── Data directory browsing / upload (file picker for FILE inputs) ─────────


def _resolve_data_path(rel_path: str) -> Optional[str]:
    """Resolve a path relative to the data dir, rejecting traversal outside it."""
    data_dir = os.path.realpath(_data_root())
    target = os.path.realpath(os.path.join(data_dir, rel_path or ''))
    if target != data_dir and not target.startswith(data_dir + os.sep):
        return None
    return target


@app.get('/api/data-files')
def list_data_files(path: str = ''):
    """List one directory level inside the work dir's data/ directory."""
    data_dir = _data_root()
    os.makedirs(data_dir, exist_ok=True)
    target = _resolve_data_path(path)
    if target is None or not os.path.isdir(target):
        return JSONResponse({'ok': False, 'msg': 'Invalid path.'}, status_code=400)

    entries = []
    for name in sorted(os.listdir(target), key=str.lower):
        full = os.path.join(target, name)
        is_dir = os.path.isdir(full)
        entries.append({
            'name': name,
            'is_dir': is_dir,
            'size': None if is_dir else os.path.getsize(full),
            'path': full,
        })
    entries.sort(key=lambda e: (not e['is_dir'],))  # folders first, stable
    rel = os.path.relpath(target, os.path.realpath(data_dir))
    return JSONResponse({'path': '' if rel == '.' else rel, 'entries': entries})


@app.post('/api/upload-data')
async def upload_data(request: Request, filename: str, path: str = ''):
    """
    Upload a file into data/<path>/. The body is the raw file content
    (streamed to disk, so large files don't sit in memory). If the client
    aborts (cancel button), the partial file is removed.
    """
    from starlette.requests import ClientDisconnect

    target_dir = _resolve_data_path(path)
    if target_dir is None:
        return JSONResponse({'ok': False, 'msg': 'Invalid path.'}, status_code=400)
    os.makedirs(target_dir, exist_ok=True)

    safe_name = os.path.basename(filename)
    if not safe_name:
        return JSONResponse({'ok': False, 'msg': 'Invalid filename.'}, status_code=400)
    dest = os.path.join(target_dir, safe_name)

    try:
        with open(dest, 'wb') as f:
            async for chunk in request.stream():
                f.write(chunk)
    except ClientDisconnect:
        if os.path.exists(dest):
            os.remove(dest)
        return JSONResponse({'ok': False, 'msg': 'Upload cancelled.'}, status_code=499)
    except Exception as exc:
        if os.path.exists(dest):
            os.remove(dest)
        return JSONResponse({'ok': False, 'msg': str(exc)}, status_code=500)

    return JSONResponse({'ok': True, 'path': dest, 'name': safe_name})


@app.post('/api/reset')
def reset_states():
    global _node_states
    _node_states = {}
    return JSONResponse({'ok': True})


@app.post('/api/clear-cache')
def clear_cache():
    import shutil as _shutil
    cfg_path = _graph_config_path()
    if not os.path.exists(cfg_path):
        return JSONResponse({'ok': False, 'msg': 'No graph config found.'})

    with open(cfg_path) as f:
        cfg = yaml.safe_load(f) or {}

    graph = KGraph(verbose=False)
    os.chdir(_workdir)
    graph.load_externals()
    try:
        graph.load_from_yaml(cfg_path)
    except Exception as exc:
        return JSONResponse({'ok': False, 'msg': str(exc)})

    uuid_val = graph.externals.get('uuid', '123456789')
    cache_root = os.path.join(_cache_root(), uuid_val)
    frozen = set(graph.frozen_nodes or [])

    cleared = []
    for node in graph.nodes:
        if node.name in frozen:
            continue
        target = os.path.join(cache_root, node.name)
        if os.path.exists(target):
            _shutil.rmtree(target)
            cleared.append(node.name)

    return JSONResponse({'ok': True, 'cleared': cleared, 'msg': f'Cleared cache for {len(cleared)} node(s).'})


@app.post('/api/stop')
def stop_pipeline():
    with _run_lock:
        alive = _run_process is not None and _run_process.poll() is None
    if alive:
        _kill_run_process()
        return JSONResponse({'ok': True, 'msg': 'Stopped.'})
    return JSONResponse({'ok': False, 'msg': 'No active run.'})


@app.get('/api/status')
def get_status():
    with _run_lock:
        running = _run_process is not None and _run_process.poll() is None
    stats = _system_stats()
    return JSONResponse({
        'running': running,
        'node_states': _node_states,
        'external_running': _external_running(),
        **stats,
    })


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    with _ws_lock:
        _ws_clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get('type') == 'stop':
                _kill_run_process()
    except WebSocketDisconnect:
        pass
    finally:
        with _ws_lock:
            if ws in _ws_clients:
                _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if os.path.exists(FRONTEND_DIST):
    app.mount('/assets', StaticFiles(directory=os.path.join(FRONTEND_DIST, 'assets')), name='assets')

    @app.get('/{full_path:path}')
    def serve_spa(full_path: str):
        index = os.path.join(FRONTEND_DIST, 'index.html')
        return FileResponse(index)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--workdir', default=os.getcwd())
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8601)
    p.add_argument('--no-browser', action='store_true')
    p.add_argument('--mcp', action='store_true',
                    help='Mount an MCP endpoint at /mcp for LLM clients')
    return p.parse_args()


def _find_free_port(host, start, tries=100):
    """First port at or above `start` that we can bind on `host`.

    SO_REUSEADDR like uvicorn's own bind: after a server is stopped (Ctrl+C) its
    closed connections keep the port in TIME_WAIT for ~60 s, and a plain bind()
    fails then although nothing listens — the next `kpl gui` drifted to 8602.
    On Linux a port with a live listener is still refused with it.
    """
    for port in range(start, start + tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if os.name != 'nt':   # on Windows it would allow binding a port in use
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
            return port
        except OSError:
            continue
    return None


def main():
    global _workdir, _loop, _bind_host

    args = _parse_args()
    _workdir = os.path.abspath(args.workdir)
    os.chdir(_workdir)
    ensure_user_config()
    _bind_host = args.host

    if args.mcp:
        try:
            from kpipeline_gui.mcp_app import mount as mount_mcp
        except ImportError:
            print("MCP support needs the optional 'mcp' package (not installed) — "
                  "continuing without it. " + mcp_install_hint())
        else:
            mount_mcp(app, _workdir)
            print(f'MCP endpoint mounted at /mcp (workdir: {_workdir})')

    # If the requested port is taken (or blocked), walk upwards to the next
    # free one: 8601 → 8602 → 8603 → …
    port = _find_free_port(args.host, args.port)
    if port is None:
        print(f'No free port found in range {args.port}–{args.port + 99}.')
        sys.exit(1)
    if port != args.port:
        print(f'Port {args.port} is in use — using port {port} instead.')

    # 0.0.0.0 listens on all interfaces but is not a browsable address
    local_url = f'http://{"localhost" if args.host == "0.0.0.0" else args.host}:{port}'

    # Start stats broadcast thread
    threading.Thread(target=_stats_loop, daemon=True).start()

    print(f'kpipeliner GUI → {local_url}')
    if args.host == '0.0.0.0':
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            lan_ip = s.getsockname()[0]
            s.close()
            print(f'On the network  → http://{lan_ip}:{port}')
        except OSError:
            pass
    print(f'Working directory: {_workdir}')

    config = uvicorn.Config(app, host=args.host, port=port, log_level='warning')
    server = uvicorn.Server(config)

    if not args.no_browser:
        # Open the browser only once the server actually accepts connections —
        # a fixed delay opens a dead tab when startup is slow (loaded machine).
        def _open_when_ready():
            for _ in range(600):  # up to 60 s
                if server.started:
                    webbrowser.open(local_url)
                    return
                time.sleep(0.1)
        threading.Thread(target=_open_when_ready, daemon=True).start()

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        # Ctrl+C: uvicorn has already shut down gracefully (port closed) and re-raises
        # the signal afterwards. No traceback for a normal stop.
        print(f'\nkpipeliner GUI stopped. Port {port} is closed.', flush=True)
    finally:
        _close_loop(_loop)


def _close_loop(loop):
    """Close the loop the way asyncio.run() does. Needed because the
    KeyboardInterrupt above leaves run_until_complete() before the loop ran its
    pending callbacks — among them anyio's "stop your worker threads" (the threads
    behind FastAPI's sync endpoints). They are not daemon threads, so the process
    then hung at exit forever, with the port already closed."""
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()


if __name__ == '__main__':
    main()
