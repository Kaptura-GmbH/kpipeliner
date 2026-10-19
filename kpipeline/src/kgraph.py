import yaml
import os
import re

from kpipeline.src.knode import KNode, NodeState
from kpipeline.src.kparameter import KParameter
from kpipeline.src.kbase_nodes import (
    get_default_base_nodes_dir, resolve_base_nodes_dirs, scan_base_nodes,
)
from kpipeline.src.kpaths import resolve_configured_dir


class KGraph:
    def __init__(self, verbose=False):
        self.nodes = []
        self.name = 'Unnamed Graph'
        self.description = ''
        self.author = ''
        self.date = ''
        self.version = '1.0'
        self.frozen_nodes = []
        # Execution-order-only edges, independent of data references:
        # [{'source': node_name, 'target': node_name}, ...]. Optional; a
        # pipeline with none behaves exactly as before this field existed.
        self.jump_links = []
        self._jump_deps = {}  # target name -> [source names], derived from jump_links
        self.execution = {'cpu_workers': 2, 'gpu_workers': 1}
        self.available_nodes = []
        self.missing_nodes = []  # [{'name', 'type'}] not found in base nodes on load
        # Per-pipeline base-node sources (graph_config.yaml):
        self.load_default_base_nodes_dir = True
        self.custom_base_nodes_dirs = []
        # Where this pipeline's exported groups are saved (graph_config.yaml `groups_dir`:
        # relative to the pipeline dir, or absolute). None = key not set (the GUI then
        # falls back to the installation's group_base_nodes dir); only written when set.
        self.groups_dir = None
        # Where this pipeline's cache/ and data/ live (graph_config.yaml
        # `cache_dir` / `data_dir`). Relative to the pipeline dir unless
        # absolute (starts with '/'). Defaults match the historical
        # 'cache' / 'data' subdirectories.
        self.cache_dir = 'cache'
        self.data_dir = 'data'
        # File logging for every node in this pipeline (graph_config.yaml
        # `log_level` / `log_enabled`) — applies uniformly, not per node.
        self.log_level = 'INFO'
        self.log_enabled = True
        # Node names marking the pipeline's inputs/outputs (metadata only —
        # execution is unaffected; the GUI colors their title bars).
        self.input_nodes = []
        self.output_nodes = []
        self._verbose = verbose
        self.externals = {}
        self.pipeliner_dir = self._find_pipeliner_dir()
        self.base_nodes_dir = get_default_base_nodes_dir(self.pipeliner_dir)
        self.load_externals()

    def _find_pipeliner_dir(self):
        # The directory of the installation-wide config.yaml (see kpaths.config_dir):
        # the repo root in a checkout, ~/.config/kpipeliner for an installed package.
        from kpipeline.src.kpaths import config_dir
        return config_dir()

    def get_base_nodes_dirs(self):
        """Effective ordered base-nodes dirs for this pipeline: the custom
        dirs from graph_config.yaml first (relative to the pipeline dir =
        cwd), then the installation default if load_default_base_nodes_dir."""
        return resolve_base_nodes_dirs(
            self.pipeliner_dir,
            load_default=self.load_default_base_nodes_dir,
            custom_dirs=self.custom_base_nodes_dirs,
        )

    def get_cache_dir(self):
        """Resolved absolute path of this pipeline's cache directory."""
        return resolve_configured_dir(os.getcwd(), self.cache_dir, 'cache')

    def get_data_dir(self):
        """Resolved absolute path of this pipeline's data directory."""
        return resolve_configured_dir(os.getcwd(), self.data_dir, 'data')

    def load_externals(self):
        self.externals = {}
        extern_dir = os.path.join(os.getcwd(), 'extern')
        if not os.path.exists(extern_dir):
            return self.externals
        for fname in os.listdir(extern_dir):
            if not fname.endswith('.yaml'):
                continue
            path = os.path.join(extern_dir, fname)
            name = os.path.splitext(fname)[0]
            try:
                data = yaml.safe_load(open(path)) or {}
                if 'value' in data:
                    self.externals[name] = data['value']
            except Exception:
                pass
        return self.externals

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load_from_yaml(self, yaml_path):
        assert os.path.exists(yaml_path), f"Graph config '{yaml_path}' not found."
        with open(yaml_path) as f:
            cfg = yaml.safe_load(f)
        if not cfg:
            return

        self.name = cfg.get('name', 'Unnamed Graph')
        self.description = cfg.get('description', '')
        self.author = cfg.get('author', '')
        self.date = cfg.get('date', '')
        self.version = str(cfg.get('version', '1.0'))
        self.frozen_nodes = cfg.get('frozen_nodes', []) or []
        self.jump_links = cfg.get('jump_links', []) or []
        self._jump_deps = {}       # target -> [source, ...] (fan-in allowed)
        self._jump_successors = {}  # source -> target (a node's jump-out feeds
                                    # at most one target; a hand-edited YAML with
                                    # two links from the same source keeps the
                                    # first and drops the rest)
        for link in self.jump_links:
            src, tgt = link.get('source'), link.get('target')
            if not (src and tgt):
                continue
            self._jump_deps.setdefault(tgt, [])
            if src not in self._jump_deps[tgt]:
                self._jump_deps[tgt].append(src)
            if src in self._jump_successors:
                if self._verbose and self._jump_successors[src] != tgt:
                    print(f"Warning: node '{src}' has more than one jump_links "
                          f"source entry; keeping '{self._jump_successors[src]}', "
                          f"ignoring '{tgt}'.")
                continue
            self._jump_successors[src] = tgt
        self.execution = cfg.get('execution', {'cpu_workers': 2, 'gpu_workers': 1}) or {}
        self.available_nodes = cfg.get('available_nodes', []) or []
        self.load_default_base_nodes_dir = bool(cfg.get('load_default_base_nodes_dir', True))
        self.custom_base_nodes_dirs = cfg.get('custom_base_nodes_dirs', []) or []
        self.groups_dir = cfg.get('groups_dir') or None
        self.cache_dir = cfg.get('cache_dir') or 'cache'
        self.data_dir = cfg.get('data_dir') or 'data'
        self.log_level = cfg.get('log_level') or 'INFO'
        self.log_enabled = bool(cfg.get('log_enabled', True))
        self.input_nodes = cfg.get('inputs', []) or []
        self.output_nodes = cfg.get('outputs', []) or []

        installed = scan_base_nodes(self.get_base_nodes_dirs())
        self.missing_nodes = []
        for node_cfg in cfg.get('nodes', []) or []:
            node_type = node_cfg.get('type', 'NONE')
            info = installed.get(node_type)
            if info is None:
                self.missing_nodes.append({
                    'name': node_cfg.get('name', node_type),
                    'type': node_type,
                })
                if self._verbose:
                    print(f"Warning: node type '{node_type}' not found in base nodes "
                          f"({self.base_nodes_dir}).")
                continue
            node_dir = info['dir']
            base_cfg_path = os.path.join(node_dir, 'node_config.yaml')

            node = KNode.__new__(KNode)
            node.name = node_cfg.get('name', node_type)
            node.type = node_type
            node.state = NodeState.PENDING
            node.error = None
            node._verbose = self._verbose
            node.inputs = {}
            node.outputs = {}
            node.description = ''
            node.category = 'General'
            node.env_manager = None
            node.env_directory = '.venv'
            node.requirements = None
            node.externals = self.externals
            node.uuid = self.externals.get('uuid', '123456789')
            node.node_dir = node_dir
            node.cache_dir = self.get_cache_dir()
            node.data_dir = self.get_data_dir()
            node.output_dir = os.path.join(node.cache_dir, node.uuid, node.name)
            node.log_level = self.log_level
            node.log_enabled = self.log_enabled
            node.gui = node_cfg.get('gui', {'position': [0, 0], 'color': None})

            node._load_from_yaml(base_cfg_path)
            node.name = node_cfg.get('name', node.name)
            node.category = info['category']

            for inp_cfg in node_cfg.get('inputs', []) or []:
                iname = inp_cfg.get('name')
                if iname and iname in node.inputs and 'value' in inp_cfg:
                    node.inputs[iname].value = inp_cfg['value']

            # Same merge as inputs above, for this node instance's group-slot
            # bindings (declared as name-only slots in node_config.yaml,
            # bound per-instance here — see KNode._load_from_yaml).
            for grp_cfg in node_cfg.get('groups', []) or []:
                gname = grp_cfg.get('name')
                if gname and gname in node.groups and 'value' in grp_cfg:
                    node.groups[gname].value = grp_cfg['value']

            # Same merge for this instance's underlying-NODE slot bindings
            # (name-only slots declared in node_config.yaml's
            # `underlying_nodes`, bound per instance here).
            node_slots = getattr(node, 'underlying_nodes', {})
            for un_cfg in node_cfg.get('underlying_nodes', []) or []:
                uname = un_cfg.get('name')
                if uname and uname in node_slots and 'value' in un_cfg:
                    node_slots[uname].value = un_cfg['value']

            self.nodes.append(node)

    def save_to_yaml(self, yaml_path):
        cfg = {
            'name': self.name,
            'description': self.description,
            'author': self.author,
            'date': self.date,
            'version': self.version,
            'execution': self.execution,
            'available_nodes': self.available_nodes,
            'frozen_nodes': self.frozen_nodes or [],
            'load_default_base_nodes_dir': self.load_default_base_nodes_dir,
            'jump_links': self.jump_links or [],
            'custom_base_nodes_dirs': self.custom_base_nodes_dirs or [],
            # only when set, so every other pipeline's graph_config.yaml stays byte-identical
            **({'groups_dir': self.groups_dir} if self.groups_dir else {}),
            'cache_dir': self.cache_dir or 'cache',
            'data_dir': self.data_dir or 'data',
            'log_level': self.log_level or 'INFO',
            'log_enabled': bool(self.log_enabled),
            'inputs': self.input_nodes or [],
            'outputs': self.output_nodes or [],
            'nodes': [],
        }
        for node in self.nodes:
            node_cfg = {
                'name': node.name,
                'type': node.type,
                'gui': getattr(node, 'gui', {'position': [0, 0], 'color': None}),
                'inputs': [
                    {'name': k, 'value': v.value}
                    for k, v in node.inputs.items()
                ],
                'outputs': [{'name': k} for k in node.outputs],
                'groups': [
                    {'name': k, 'value': v.value}
                    for k, v in getattr(node, 'groups', {}).items()
                ],
            }
            # Only for node types that DECLARE underlying-node slots: unlike
            # `groups` above (written as [] for every node), an always-present
            # key here would change every existing pipeline's file on its next
            # save.
            node_slots = getattr(node, 'underlying_nodes', {})
            if node_slots:
                node_cfg['underlying_nodes'] = [
                    {'name': k, 'value': v.value}
                    for k, v in node_slots.items()
                ]
            cfg['nodes'].append(node_cfg)
        with open(yaml_path, 'w') as f:
            yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)
        if self._verbose:
            print(f"Graph saved to '{yaml_path}'.")

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def add_node(self, node):
        if not isinstance(node, KNode):
            raise TypeError('node must be a KNode instance')
        self.nodes.append(node)

    def remove_node(self, node_name):
        self.nodes = [n for n in self.nodes if n.name != node_name]

    def get_node(self, name):
        return next((n for n in self.nodes if n.name == name), None)

    def count_nodes(self, node_type=None):
        if node_type:
            return sum(1 for n in self.nodes if n.type == node_type)
        return len(self.nodes)

    # ------------------------------------------------------------------
    # Dependency analysis
    # ------------------------------------------------------------------

    _ref_re = re.compile(r'^nodes\.(\w+)\.(\w+)$')

    def get_dependencies(self, node_name):
        """Return list of node names that node_name directly depends on.

        Includes both data-reference dependencies (input values matching
        `nodes.<name>.<output>`) and optional jump_links (execution-order-only,
        no data)."""
        node = self.get_node(node_name)
        if not node:
            return []
        deps = []
        for param in node.inputs.values():
            if isinstance(param.value, str):
                m = self._ref_re.match(param.value)
                if m:
                    dep = m.group(1)
                    if dep not in deps:
                        deps.append(dep)
        for dep in self.get_jump_predecessors(node_name):
            if dep not in deps:
                deps.append(dep)
        return deps

    def get_jump_predecessors(self, node_name):
        """Return list of node names node_name must run after via jump_links
        only (no data dependency). Supports fan-in: a node may have several
        jump predecessors."""
        return list(self._jump_deps.get(node_name, []))

    def get_jump_successors(self, node_name):
        """Return list of node names to run after node_name finishes, via
        jump_links only (no data dependency). A node's jump-out feeds at most
        one target, so this is at most a single-element list (kept as a list
        for a uniform call shape with get_jump_predecessors)."""
        tgt = self._jump_successors.get(node_name)
        return [tgt] if tgt else []

    def get_kwui_sequence(self):
        """Linear node sequence for a kwui run: start at each declared input
        node (`inputs:`) and follow its jump-out chain until a node has none.

        A jump-out feeds at most one target, so the walk can only ever produce
        a sequence, never a branching graph — which is what makes a single
        "current step" tracker well-defined. Data dependencies are deliberately
        NOT part of this list; they still get pulled in and run by run_node()
        on demand, they just don't appear as their own tracker step.
        """
        sequence = []
        seen = set()
        for start in self.input_nodes:
            current = start
            while current and current not in seen and self.get_node(current):
                seen.add(current)
                sequence.append(current)
                successors = self.get_jump_successors(current)
                current = successors[0] if successors else None
        return sequence

    def get_all_required(self, node_name):
        """Return ordered list of all nodes needed to run node_name."""
        return self.get_all_required_multi([node_name])

    def get_all_required_multi(self, node_names):
        """Return ordered list of all nodes needed to run every node in
        node_names (union of their transitive dependency closures, still in
        a valid dependency order since `visited` is shared across roots)."""
        visited = []

        def collect(name):
            if name in visited:
                return
            for dep in self.get_dependencies(name):
                collect(dep)
            if name not in visited:
                visited.append(name)

        for name in node_names:
            collect(name)
        return visited

    def get_run_order(self):
        """Nodes to run for a full `kpl run` (no explicit node given): only
        those needed to produce the pipeline's declared outputs (`outputs:`
        in graph_config.yaml) plus their transitive dependencies. No outputs
        declared -> nothing to run."""
        valid_outputs = [n for n in self.output_nodes if self.get_node(n)]
        if not valid_outputs:
            return []
        return self.get_all_required_multi(valid_outputs)

    def get_execution_order(self):
        """Topological sort for the entire graph (Kahn's algorithm)."""
        in_degree = {n.name: 0 for n in self.nodes}
        adj = {n.name: [] for n in self.nodes}

        for node in self.nodes:
            for dep in self.get_dependencies(node.name):
                if dep in adj:
                    adj[dep].append(node.name)
                    in_degree[node.name] += 1

        queue = [name for name, deg in in_degree.items() if deg == 0]
        order = []
        while queue:
            name = queue.pop(0)
            order.append(name)
            for child in adj[name]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(self.nodes):
            # Cycle detected – return original order as fallback
            return [n.name for n in self.nodes]
        return order

    def is_value_reference(self, value):
        return isinstance(value, str) and bool(self._ref_re.match(value))

    def extract_node_param_id_from_reference(self, value):
        m = self._ref_re.match(value) if isinstance(value, str) else None
        if m:
            return m.group(1), m.group(2)
        return None, None

    # ------------------------------------------------------------------
    # Available node types
    # ------------------------------------------------------------------

    def list_available_node_types(self):
        """Return list of dicts describing available node types.

        The category of a node is the name of the directory it lives in
        inside its base-nodes dir (nodes directly in a root get
        "no category"). Scans the pipeline's effective dirs (custom first,
        then the default when enabled)."""
        result = []
        for entry, info in scan_base_nodes(self.get_base_nodes_dirs()).items():
            if self.available_nodes and entry not in self.available_nodes:
                continue
            cfg_path = os.path.join(info['dir'], 'node_config.yaml')
            try:
                cfg = yaml.safe_load(open(cfg_path)) or {}
            except Exception:
                continue
            result.append({
                'type': entry,
                'description': cfg.get('description', ''),
                'category': info['category'],
                'inputs': [i['name'] for i in cfg.get('inputs', []) or []],
                'input_types': {
                    i['name']: (i.get('type') or 'STRING')
                    for i in cfg.get('inputs', []) or []
                },
                'outputs': [o['name'] for o in cfg.get('outputs', []) or []],
                'default': {
                    i['name']: i.get('default', i.get('Default'))
                    for i in cfg.get('inputs', []) or []
                    if 'default' in i or 'Default' in i
                },
                'caption': cfg.get('caption', ''),
                # Marks a node type that builds a web UI via self.ui in a
                # `kpl run --kwui` run; the GUI badges it in the palette.
                'kwui': bool(cfg.get('kwui', False)),
                # Declared group SLOTS (node_config.yaml's underlying_groups —
                # same {name: ...}-dict-or-bare-string tolerance as
                # kpipeline_gui/server.py's _node_type_cfg and
                # KNode._load_from_yaml). Lets the GUI populate a freshly
                # dropped node's own data.underlying_groups (all slots
                # unbound) at CREATION time, same as `kwui` above already
                # does for is_kwui — without this, a new instance shows no
                # slots in its Properties panel until the next /api/graph
                # load, since that's otherwise the only place bindings
                # (not just names) get resolved per-instance. `description`
                # (shown in the Properties panel) and `address` (a group
                # component's category/name — checked at node-creation time
                # so the GUI can auto-instantiate/bind that component) are
                # both optional and only meaningful on the dict form.
                'underlying_groups': [
                    {
                        'name': (g.get('name') if isinstance(g, dict) else g),
                        'description': (g.get('description', '') if isinstance(g, dict) else ''),
                        'address': (g.get('address') if isinstance(g, dict) else None),
                    }
                    for g in (cfg.get('underlying_groups', []) or [])
                    if (g.get('name') if isinstance(g, dict) else g)
                ],
                # Declared underlying-NODE slots (node_config.yaml's
                # underlying_nodes) — same tolerance and purpose as
                # underlying_groups above; `type` (optional) is the node type
                # a slot expects, used by the GUI to filter its dropdown.
                'underlying_nodes': [
                    {
                        'name': (n.get('name') if isinstance(n, dict) else n),
                        'description': (n.get('description', '') if isinstance(n, dict) else ''),
                        'type': (n.get('type') if isinstance(n, dict) else None),
                    }
                    for n in (cfg.get('underlying_nodes', []) or [])
                    if (n.get('name') if isinstance(n, dict) else n)
                ],
            })
        return result
