import re
import os
import yaml


class KParameter:
    TYPES = ('STRING', 'INT', 'FLOAT', 'BOOL', 'FILE', 'DIR', 'ANY')

    # Sentinel distinguishing "value not yet lazily resolved" from a real None.
    _UNSET = object()

    def __init__(self, name, value=None, ptype=None, format=None, description=None):
        self.name = name
        self._value = value
        self.type = ptype or 'ANY'
        self.format = format
        self.description = description
        self._ref_pattern = re.compile(r'^nodes\.(\w+)\.(\w+)$')
        # Set only inside a node.py subprocess, only for inputs whose value
        # came from a `nodes.X.Y` reference (see KNode._init_argparser /
        # SourceNodeRef in knode.py). None for every other KParameter use
        # (outputs, literal inputs, parent-side graph objects).
        self.source_node = None

    def __str__(self):
        return f"KParameter(name={self.name}, value={self.value}, type={self.type})"

    @property
    def value(self):
        if self._value is KParameter._UNSET:
            # Lazy read: only reachable when source_node was set via
            # set_source_node() below.
            self._value = self.source_node.get_value()
        return self._value

    @value.setter
    def value(self, v):
        self._value = v

    def set_source_node(self, source_node):
        """Bind this input to a SourceNodeRef (knode.py) instead of a literal
        value. The actual cache read is deferred to the first `.value` access
        (or forced fresh by SourceNodeRef.rerun())."""
        self.source_node = source_node
        self._value = KParameter._UNSET

    def is_reference(self):
        return isinstance(self.value, str) and bool(self._ref_pattern.match(self.value))

    def resolve_reference(self):
        """Returns (node_id, output_name) or (None, None)."""
        if not self.is_reference():
            return None, None
        m = self._ref_pattern.match(self.value)
        return m.group(1), m.group(2)

    def get_cached_value(self, uuid, node_id, cache_root):
        """cache_root – the pipeline's resolved cache directory (KGraph.get_cache_dir()),
        not the pipeline dir itself: the cache dir is independently configurable
        via graph_config.yaml's `cache_dir` key."""
        cache_dir = os.path.join(cache_root, uuid, node_id)
        outputs_yaml = os.path.join(cache_dir, 'outputs.yaml')
        if not os.path.exists(outputs_yaml):
            return None
        with open(outputs_yaml) as f:
            data = yaml.safe_load(f) or {}
        val = data.get(self.name)
        if val is not None:
            self.value = val
        return val
