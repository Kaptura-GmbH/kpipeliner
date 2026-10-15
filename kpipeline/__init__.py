from kpipeline.src.kparameter import KParameter
from kpipeline.src.knode import KNode, NodeState
from kpipeline.src.kgraph import KGraph
from kpipeline.src.executor import PipelineExecutor

__all__ = ['KParameter', 'KNode', 'KGraph', 'NodeState', 'PipelineExecutor']

try:
    from importlib.metadata import PackageNotFoundError, version as _version
    __version__ = _version('kpipeliner')
except Exception:  # not installed (plain checkout on sys.path)
    __version__ = 'unknown'
