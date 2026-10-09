"""
Resolution helpers for the pipeline's configurable cache/ and data/
directories (graph_config.yaml keys `cache_dir` / `data_dir`).

A configured value is used as-is if it's an absolute path (starts with
'/'), otherwise it's resolved relative to the pipeline dir (cwd).
"""
import os
import yaml


def resolve_configured_dir(pipeline_dir, configured, default_name):
    value = configured or default_name
    if os.path.isabs(value):
        return value
    return os.path.join(pipeline_dir, value)


def read_configured_dirs(pipeline_dir, graph_config_name='graph_config.yaml'):
    """Resolved (cache_dir, data_dir) read straight from graph_config.yaml,
    for call sites that need them without a full KGraph load (e.g. the GUI
    server's cache/data endpoints, Pipeliner._write_run_state)."""
    cache_name, data_name = 'cache', 'data'
    cfg_path = os.path.join(pipeline_dir, graph_config_name)
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            cache_name = cfg.get('cache_dir') or 'cache'
            data_name = cfg.get('data_dir') or 'data'
        except Exception:
            pass
    return (
        resolve_configured_dir(pipeline_dir, cache_name, 'cache'),
        resolve_configured_dir(pipeline_dir, data_name, 'data'),
    )
