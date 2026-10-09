# kpipeliner

A node-based pipeline manager. A pipeline is a graph of nodes; each node is a
standalone Python program (its own subprocess, optionally its own
venv/conda/pixi environment) with typed inputs and outputs declared in YAML.
Build and run pipelines from the CLI (`kpl`), a visual web GUI, or hand them
over to an LLM through MCP.

![kpipeliner GUI](docs/wiki/images/gui-overview.png)

## Highlights

- **Graph = YAML.** Connections are just strings (`nodes.<name>.<output>`) —
  no hidden edge list, easy to read, easy to script.
- **Subprocess isolation.** Every node runs on its own, with its own Python
  environment if it needs one; a crash in one node never takes down another.
- **Caching by default.** Node outputs are cached per run-UUID; re-running a
  pipeline only recomputes what actually changed.
- **CLI + GUI + MCP.** Script it, click through it in the browser, or connect
  an MCP client and let an LLM design/inspect/run it for you.
- **Groups & reusable components.** Bundle nodes in the GUI and export them as
  drag-and-drop components for other pipelines.

## Install

```bash
git clone git@gitlab.com:KapturaGmbH/project44/kaptura-pipeliner.git
cd kaptura-pipeliner
./install.sh                      # uses your default python environment
# or: ./install.sh --venv         # creates/uses a venv at ./.venv instead
source ~/.bashrc                  # or restart your terminal
kpl --help
```

The installer sets up the Python environment, installs dependencies, builds
the GUI frontend (needs Node.js/npm — the CLI still works without it), and
adds `kpl` to your `PATH`. MCP support is a separate, optional install (see
the table below):

```bash
pip install -r requirements-mcp.txt
```

## Quick start

```bash
mkdir my_pipeline && cd my_pipeline
kpl init                 # creates graph_config.yaml, data/, cache/
kpl list                 # see available node types
kpl add <node_type>      # add a node instance
kpl tree                 # inspect the graph
kpl run                  # run it (or: kpl gui to do all of this visually)
```

## Commands

| Command | Description |
|---|---|
| `kpl init` | Initialize a pipeline in the current directory |
| `kpl gui` | Launch the web GUI (port 8601+) |
| `kpl gui --mcp` | GUI + an MCP endpoint at `/mcp`, same port |
| `kpl mcp` | Headless MCP server only — no GUI/REST API |
| `kpl list` | List available node types |
| `kpl add <type> [name]` | Add a node instance to the graph |
| `kpl run [node]` | Run the declared outputs (or one node) + dependencies |
| `kpl run_debug [node]` | Same, with full log output |
| `kpl run --kwui` | Run behind a per-node web UI (kwebui) |
| `kpl tree` | Print the graph with dependencies and I/O |
| `kpl status` | Show cached/frozen state per node |
| `kpl clean [node]` | Clear cache (all nodes, or one) |
| `kpl kwui init` | Create `workflow.py` for kwui mode in this pipeline |
| `kpl base_node create` | Scaffold a new node type in the current directory |
| `kpl base_node install [--node_name N]` | Install the current directory as a node type |
| `kpl base_node uninstall <name>` | Remove an installed node type |
| `kpl node_env install [--node_type T]` | Install a node's Python environment |
| `kpl uninstall` | Remove kpl from your PATH |

Run any command with `--help` for its full option list.

## Documentation

- **[User Guide](docs/wiki/User-Guide.md)** — the full walkthrough: CLI and
  GUI workflows, groups, custom nodes, MCP, kwui mode, and the
  `graph_config.yaml`/`node_config.yaml`/`config.yaml` reference.
- **[kpipeliner.md](kpipeliner.md)** — a self-contained agent guide for
  building pipelines/nodes (also installed as a Claude Code skill).
- **[Readme](Readme)** — version history / changelog.
