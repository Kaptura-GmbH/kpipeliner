"""
Resolution helpers for the pipeline's configurable cache/ and data/
directories (graph_config.yaml keys `cache_dir` / `data_dir`).

A configured value is used as-is if it's an absolute path (starts with
'/'), otherwise it's resolved relative to the pipeline dir (cwd).
"""
import importlib.util
import os
import sys

import yaml

#: <root>/kpipeline — the package directory (templates live in <PACKAGE_DIR>/templates).
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The directory that CONTAINS the `kpipeline` and `kpipeline_gui` packages: the repo
#: root in a source checkout, `site-packages` in a pip/uv install. Code assets
#: (templates, the GUI server script) are found relative to it in both layouts.
PIPELINER_DIR = os.path.dirname(PACKAGE_DIR)


def is_checkout():
    """True when running from a source checkout (pyproject.toml sits next to the
    packages — also true for an editable install of it); False for a regular
    pip/uv install, whose site-packages has no pyproject.toml."""
    return os.path.exists(os.path.join(PIPELINER_DIR, 'pyproject.toml'))


def config_dir():
    """The directory of the installation-wide `config.yaml` — and the anchor for its
    relative paths (`base_nodes/`, `group_base_nodes/`).

    `KPL_HOME` overrides everything. A source checkout keeps using its own root, exactly
    as before. An installed package has no writable place next to its code, so it uses
    `$XDG_CONFIG_HOME/kpipeliner` (default `~/.config/kpipeliner`); the directory is
    created when something is first written there, and a missing config.yaml just
    means the built-in defaults."""
    home = os.environ.get('KPL_HOME')
    if home:
        return os.path.abspath(os.path.expanduser(home))
    if is_checkout():
        return PIPELINER_DIR
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'kpipeliner')


def config_path():
    return os.path.join(config_dir(), 'config.yaml')


def packaged_config_path():
    """The default config.yaml that ships inside the package (read-only template)."""
    return os.path.join(PACKAGE_DIR, 'config.yaml')


def ensure_user_config():
    """Make sure the installation's config.yaml exists where config_path() says, copying
    the packaged default there on first use so the user can edit it (and keep the edits
    across package upgrades). A source checkout without KPL_HOME has its own config.yaml
    in the repo root — left exactly as it is, never created. Best effort: a read-only
    home just means the built-in defaults apply. Returns the path, or None."""
    path = config_path()
    if os.path.exists(path):
        return path
    if is_checkout() and not os.environ.get('KPL_HOME'):
        return None
    try:
        import shutil
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(packaged_config_path(), path)
        return path
    except OSError:
        return None


#: How to start the CLI by module name. NEVER by file path: the CLI file is
#: kpipeline/kpipeline.py, and running a file puts its own folder first on sys.path, so
#: `import kpipeline` would then find that file instead of the package.
KPL_MODULE = 'kpipeline.kpipeline'


def kpl_command():
    """argv prefix that starts the `kpl` CLI as a subprocess (used by the GUI server):
    the `kpl` script of this Python environment (a pip/uv install, or an editable
    install of a checkout), else the checkout's `kpl` launcher, else
    `python -m kpipeline.kpipeline`."""
    script = os.path.join(os.path.dirname(sys.executable), 'kpl')
    if os.path.exists(script):
        return [script]
    if is_checkout():
        for name in ('kpl', 'kpipeliner'):  # `kpipeliner` is the legacy launcher name
            launcher = os.path.join(PIPELINER_DIR, name)
            if os.path.exists(launcher):
                return [launcher]
    return [sys.executable, '-m', KPL_MODULE]


def kpl_python_command():
    """Like kpl_command(), but always through THIS interpreter — what the MCP tool
    bodies use."""
    return [sys.executable, '-m', KPL_MODULE]


def kpl_env(env=None):
    """Environment for a `kpl` subprocess. In a source checkout the directory that holds
    the `kpipeline` package is put on PYTHONPATH, so `python -m kpipeline.kpipeline` also
    works from a checkout that is not installed. For an installed package nothing is
    added: PIPELINER_DIR is then site-packages, and exporting it would be inherited by
    every node and shadow the packages of a node's own venv (numpy, ...)."""
    env = dict(os.environ if env is None else env)
    if is_checkout():
        env['PYTHONPATH'] = PIPELINER_DIR + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    return env


def node_import_path():
    """A directory that contains ONLY the `kpipeline` package, for node processes that
    run in their own venv/conda env (where `from kpipeline import KNode` would otherwise
    fail). Nodes used to find the package through `sys.path.append(dirname(which('kpl')))`,
    which only worked while the launcher sat next to the package in the checkout; an
    installed `kpl` lives in a bin/ directory. Exposing site-packages itself would let the
    kpl environment's packages shadow the node's own, so this is a directory holding just a
    symlink `kpipeline -> <package>` under the user cache. Returns None if it can't be made
    (a checkout then falls back to its root, which holds only the kpipeliner packages)."""
    import hashlib
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    key = hashlib.sha1(PACKAGE_DIR.encode()).hexdigest()[:10]
    shim = os.path.join(base, 'kpipeliner', 'pypath-' + key)
    link = os.path.join(shim, 'kpipeline')
    try:
        if os.path.realpath(link) != os.path.realpath(PACKAGE_DIR):
            os.makedirs(shim, exist_ok=True)
            if os.path.lexists(link):
                os.remove(link)  # only ever our own symlink, in our own cache dir
            os.symlink(PACKAGE_DIR, link, target_is_directory=True)
        return shim
    except OSError:
        return PIPELINER_DIR if is_checkout() else None


def mcp_available():
    """Whether the optional MCP SDK this project needs (`mcp>=2`, which provides
    `mcp.server.mcpserver`) is importable. Checking the submodule — not just `mcp` —
    means an older `mcp` 1.x is reported as missing instead of failing later."""
    try:
        return importlib.util.find_spec('mcp.server.mcpserver') is not None
    except (ImportError, ValueError):
        return False


def mcp_install_hint():
    """The 'Install it with: ...' line shown when MCP support is requested but missing."""
    if is_checkout():
        return (f'Install it with: {sys.executable} -m pip install -r '
                f"{os.path.join(PIPELINER_DIR, 'requirements-mcp.txt')}")
    return ('Install it with: uv pip install "kpipeliner[mcp]"   '
            f'(or: {sys.executable} -m pip install "kpipeliner[mcp]")')


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
