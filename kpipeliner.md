# kpipeliner — Agent Guide

You are working in a project that uses **kpipeliner** (`kpl`), a node-based pipeline
manager. This file is self-contained: everything you need to create pipelines and
node types is specified here — you do not need the kpipeliner source code.

**Assumption:** `kpl` is installed and on PATH. Verify with `kpl --help`.
If it is missing, stop and tell the user to install kpipeliner first.

---

## 1. Model

- A **pipeline** is a directory containing `graph_config.yaml` (the graph), `data/`
  (shared input files), `cache/` (results), and `extern/uuid.yaml` (a unique id that
  namespaces the cache). `kpl` always operates on the **current working directory**.
  `data/` and `cache/` can be relocated via `data_dir` / `cache_dir` in
  `graph_config.yaml` (relative to the pipeline dir, or absolute).
- A **node type** is a directory with exactly two required files: `node_config.yaml`
  (typed inputs/outputs + metadata) and `node.py` (the logic). Each node runs as its
  own subprocess, optionally in its own Python environment.
- A **node instance** is an entry in `graph_config.yaml` referencing a node type.
- **Connections are strings**: an input whose value matches
  `nodes.<node_name>.<output_name>` is a reference to another node's output.
  Dependency order, recursive execution, and GUI wires are all derived from this
  pattern alone. There is no separate edge list.
- **Cache contract**: a node saves each output to
  `cache/<uuid>/<node_name>/outputs.yaml` via `self.save_output()`. Downstream
  inputs are resolved from that file. Runs reuse cached results by default.
- **Names must match `\w+`** (letters, digits, underscore) — node names, input
  names, output names. Anything else breaks reference parsing.

---

## 2. Create a pipeline

```bash
mkdir my_pipeline && cd my_pipeline
kpl init                    # creates graph_config.yaml, data/, cache/, extern/uuid.yaml
kpl list                    # available node types (grouped by category)
kpl add <node_type>         # appends instance <node_type>_1 with default inputs
kpl add <node_type> myname  # or with an explicit name
```

Then edit `graph_config.yaml`. Full schema:

```yaml
name: "my_pipeline"
description: ""
author: ""
date: ""
version: "1.0"

execution:
  cpu_workers: 2            # max parallel CPU nodes
  gpu_workers: 1            # max parallel GPU nodes (node category gpu/training/inference)

available_nodes: []         # optional: restrict node types offered in the GUI
frozen_nodes: []            # node names whose cache is never cleared (kpl clean skips them)

# Where node types are loaded from:
load_default_base_nodes_dir: true   # include the machine-wide default node dir
custom_base_nodes_dirs: []          # extra dirs with node types; absolute or relative
                                    # to this pipeline dir; scanned BEFORE the default,
                                    # so a custom type wins on a name clash
groups_dir: groups                  # where groups exported from the GUI are saved (and read first);
                                    # absolute or relative to this pipeline dir. Unset -> the
                                    # installation's group_base_nodes dir (config.yaml)

cache_dir: cache             # where results (cache/<uuid>/<node>/outputs.yaml) live;
                              # relative to this pipeline dir, or absolute (starts with '/')
data_dir: data                # where shared input files live; relative or absolute, as above

log_level: INFO              # DEBUG | INFO | OUTPUT | WARNING | ERROR — applies to every node's
                              # self.log()/print() in this pipeline (not per-node); log file at
                              # cache/<uuid>/<node>/log/run.log, appended across runs
log_enabled: true            # false disables file logging entirely (self.log() still prints)

inputs: []                  # node names marked as pipeline inputs  (metadata only)
outputs: []                 # node names marked as pipeline outputs — NOT metadata-only:
                              # `kpl run`/`run_debug` with no node argument only runs these
                              # nodes plus whatever they transitively depend on. Empty list
                              # → nothing runs. `kpl run <node>` (explicit node) is unaffected.

jump_links: []               # optional: [{source: node_name, target: node_name}, ...]
                              # execution-order-only edges — target runs after source,
                              # no data exchanged. Not a node input/output. A target can
                              # have several jump_links entries (waits on all of them,
                              # fan-in); a source can appear in only ONE entry (a node's
                              # jump-out feeds a single target, no fan-out).
                              # In the GUI: hover a node to reveal its jump-out handle
                              # (diamond, right edge of the title bar, same x as a regular
                              # output port); drag to another node — while dragging, the
                              # hovered node reveals its jump-in handle (left edge, same x
                              # as a regular input port) to connect.
                              # After a node finishes, it automatically runs whatever its
                              # jump-out points to (chained side effect, not just an
                              # ordering constraint on the target).

nodes:
  - name: loader_1          # unique, \w+ only
    type: my_loader         # a node type name from kpl list
    gui:                    # optional; GUI position/color
      position: [100, 100]
      color: null
    inputs:
      - name: src_file
        value: data/input.txt              # plain value
    outputs:                # names only; values live in the cache
      - name: out_dir

  - name: process_1
    type: my_processor
    inputs:
      - name: in_dir
        value: nodes.loader_1.out_dir      # ← connection (reference)
      - name: iterations
        value: 5
    outputs:
      - name: result
```

Rules:

- Reference syntax is exactly `nodes.<node_name>.<output_name>` — no spaces, no
  nesting, the whole string must match.
- You never order nodes manually: running a node first runs (or reads from cache)
  everything it references, recursively.
- `inputs:` (top level) is metadata only — documents the pipeline's entry points
  (the GUI colors them brown). `outputs:` documents exit points (green) AND scopes
  `kpl run`/`run_debug` with no explicit node: only the declared outputs and their
  transitive dependencies run; other nodes in the graph are left untouched. Empty
  `outputs:` means such a run does nothing. Running a specific node by name
  (`kpl run <node>`) always ignores this and runs just that node's own dependencies.
- `jump_links:` (top level) is the only way to force run-after ordering with no
  data reference between two nodes; it participates in the same dependency graph
  (topological order, cycle avoidance, fail→skip-dependents) as real references
  for full-graph runs. Running a specific node by name (`kpl run <node>`) also
  pulls in its jump predecessor(s), but only if one hasn't completed before —
  same "does it have cache" question `kpl status`'s `[cached]` marker answers,
  gated by `--no-use-cache` exactly like a real reference. A jump-out also
  still fires forward automatically once its source node finishes (a chain
  keeps going on its own either way you enter it).

Inspect and run:

```bash
kpl tree                    # nodes + dependencies + inputs/outputs
kpl status                  # which nodes have cached results / are frozen
kpl run                     # declared outputs + their deps, parallel, cache-aware
                              # (nothing runs if outputs: is empty)
kpl run process_1           # one node + its dependencies
kpl run --no-use-cache      # force recompute
kpl run_debug [node]        # full node output (use when a node fails)
kpl kwui init               # create workflow.py for the kwebui workflow UI (see §7)
kpl kwui design <node_type> # design a node's UI page visually (kwebui edit mode, see §7.5)
kpl run --kwui              # run sequentially behind that UI (--kwui-host/--kwui-port)
kpl run process_1 --params iterations 10   # override an input for this run
kpl clean [node]            # clear cache (all non-frozen nodes, or one node)
kpl run --clean [node]      # clear the node's cache (no node: every node of the run; frozen kept), then run; also run_debug. Flag goes BEFORE the node; not with --kwui
kpl gui                     # web GUI for the current directory (port 8601+)
```

`kpl run` exits non-zero if any node fails; dependents of a failed node are skipped.
Read a node's results from `cache/<uuid>/<node_name>/outputs.yaml`
(uuid is in `extern/uuid.yaml`).

---

## 3. Create a node type

A node type is a directory (its name = the type name, `\w+` only):

```
my_processor/
├── node_config.yaml
└── node.py
```

### 3.1 `node_config.yaml`

```yaml
author: "Author"
date: "2026-01-01"
version: "1.0"
description: "One line describing what this node does."   # shown in kpl list / GUI
category: "General"     # GUI grouping; category gpu|training|inference => uses a GPU worker slot
kwui: false             # optional: true => this node builds a web UI via self.ui (see §7)
# on_error: skip        # optional, kwui runs only (see §7.3): a CRASH of this node type is shown on
#                       # its step but the workflow carries on, like self.ui.end("ERROR_SKIP");
#                       # default "stop". An explicit self.ui.end("ERROR") always stops.

inputs:
  - name: src_file          # \w+ only
    type: FILE              # STRING | INT | FLOAT | BOOL | FILE | DIR
    default: ""
    description: "Input file to process."
  - name: iterations
    type: INT
    default: 3
  - name: verbose
    type: BOOL
    default: false

outputs:
  - name: result            # \w+ only
    type: STRING
  - name: out_dir
    type: DIR

# Only if the node needs its own packages (omit otherwise — the node then runs
# in kpipeliner's own Python environment):
# python_environment:
#   env_manager: venv       # null | venv | conda | pixi
#   env_directory: .venv    # created inside the node dir for venv
#   requirements: requirements.txt

# Only if this node type should drive a whole GROUP as a subroutine (§3.6):
# underlying_groups:
#   - name: calibration        # self.groups['calibration'] in node.py
#     description: "Group that produces the calibration file this node needs."
#     address: precalibration/camera_setup   # optional: category/name of a
#                                             # group component to auto-instantiate

# Only if this node type should run OTHER NODES itself, when it decides (§3.7):
# underlying_nodes:
#   - name: table              # self.nodes['table'] in node.py
#     description: "Turntable controller this node drives."
#     type: table_ctrl         # optional: node type this slot expects
```

### 3.2 `node.py`

```python
import os
import shutil
import sys

# Make the kpipeline package importable from the installed kpl location.
sys.path.append(os.path.dirname(shutil.which("kpl") or shutil.which("kpipeliner")))

from kpipeline import KNode


class KapNode(KNode):

    def run(self):
        # 1. Read inputs (references are already resolved to concrete values)
        src = self.inputs["src_file"].value
        iterations = int(self.inputs["iterations"].value)

        self.log(f"processing {src}")
        self.send_status_message("RUNNING", progress=0, max=iterations,
                                 message="starting")

        # 2. Work. ALL result files go into self.output_dir.
        result_path = os.path.join(self.output_dir, "result.txt")
        with open(result_path, "w") as f:
            for i in range(iterations):
                f.write(f"step {i}\n")
                self.send_status_message("RUNNING", progress=i + 1,
                                         max=iterations, message=f"step {i + 1}")

        # 3. Save EVERY output declared in node_config.yaml.
        self.save_output("result", result_path)
        self.save_output("out_dir", self.output_dir)

        self.send_status_message("COMPLETED", message="done")


if __name__ == "__main__":
    KapNode(arg_parser=True)      # required exactly like this
```

### 3.3 KNode API (available inside `run()`)

| Member | Meaning |
|---|---|
| `self.inputs['name'].value` | Input value. Type is NOT auto-cast except INT/FLOAT via CLI — cast defensively. |
| `self.output_dir` | `cache/<uuid>/<node_name>/` — write all result files here, nowhere else. |
| `self.data_dir` | The pipeline's shared `data/` directory (read input files from here). |
| `self.name` / `self.uuid` | Node instance name / pipeline uuid. |
| `self.save_output(name, value)` | REQUIRED for every declared output. Persists to `outputs.yaml`; this is what downstream nodes receive. Values must be YAML-serializable (use file paths, not objects). |
| `self.save_image(name, img)` | Save a numpy/cv2 image to output_dir and register its path as output. |
| `self.log(msg, level='info')` | Log line (`debug`, `info`, `warning`, `error`) — visible in terminal/GUI AND appended to `output_dir/log/run.log`, backed by Python's `logging` module. `debug` is hidden unless the pipeline's `log_level: DEBUG`. A bare `print(...)` is captured the same way, logged as `info`. |
| `self.send_status_message("RUNNING", progress=i, max=n, message=...)` | Drives progress bars. Send `"COMPLETED"` at the end. Also recorded in `log/run.log` (not duplicated in the terminal — it's already visible there via the progress bar). |
| `self.make_dir("sub")` | Create and return a subdirectory of `output_dir`. |
| `self.ui` | The node's panel in the kwui workflow UI — `None` unless `node_config.yaml` sets `kwui: true`. See §7. |
| `self.groups['slot']` | Handle to a group declared in `underlying_groups` and bound (in the GUI's Properties panel) to a real group in this pipeline. See §3.6. |
| `self.nodes['slot']` | Handle to a node declared in `underlying_nodes` and bound (in `graph_config.yaml`) to a real node in this pipeline — run it whenever, as often as you like. See §3.7. |

Failure convention: raise an exception or `sys.exit(1)` — any non-zero exit code
marks the node FAILED and stops dependents. An uncaught exception is logged once
(traceback, `error` level, file + terminal) before the node exits non-zero.

Logging is pipeline-wide, not per node: `graph_config.yaml`'s `log_level` (default `INFO`)
and `log_enabled` (default `true`) apply to every node's `self.log()`/`print()` in that run.

### 3.4 Test the node standalone

`node.py` is a normal CLI program; every input is a `--flag`:

```bash
python node.py --node_id test_1 --src_file some.txt --iterations 2
# BOOL inputs are +/- flags (argparse BooleanOptionalAction):
# --verbose = true, --no-verbose = false, omitted = the node type's declared default
```

Run it from a pipeline-like directory (it writes to `./cache/...` relative to cwd).
Check `cache/123456789/test_1/outputs.yaml` afterwards (123456789 is the default
uuid when none is passed).

### 3.5 Make the type available to a pipeline

Preferred for project-local nodes — keep them in your project and register the
directory in the pipeline's `graph_config.yaml`:

```yaml
custom_base_nodes_dirs:
  - ../my_project_nodes     # relative to the pipeline dir, or absolute
```

The layout inside such a dir: either node dirs directly at the root (category
"no category"), or one level of category dirs containing node dirs. A dir is a
node iff it contains `node_config.yaml`; dirs starting with `_` or `.` are skipped.

```
my_project_nodes/
└── processing/             # category (optional level)
    └── my_processor/
        ├── node_config.yaml
        └── node.py
```

Alternative — install machine-wide into the default node dir:

```bash
cd my_processor && kpl base_node install
```

If the node declares a `python_environment`, install it once:

```bash
kpl node_env install --node_type my_processor
```

### 3.6 Underlying groups — `self.groups[...]`

A node type can declare named group *slots* (`underlying_groups` in
`node_config.yaml`, shown in §3.1) — like declaring an input, except the
"value" bound per pipeline instance is a whole GUI group, not a single
value. Binding happens per node instance in that pipeline's Properties
panel (or `graph_config.yaml`'s `groups: [{name, value}]` list on the node,
mirroring `inputs:`); it has no effect on execution by itself.

Unlike a plain `nodes.X.Y` reference (implicit, execution-order owned by the
graph), an underlying group is explicit and meant to be driven like a
subroutine from inside `run()`:

```python
def run(self):
    calib = self.groups['calibration']
    calib.inputs['exposure'] = 120     # one-off override — NOT saved to graph_config.yaml
    if not calib.run():                # blocking; True/False
        sys.exit(1)
    calib_file = calib.outputs['calibration_file']   # read after a successful run
    calib.remove_cache()               # blocking; True/False — clears every node in the group
```

| Member | Meaning |
|---|---|
| `.inputs['port'] = value` | Stage a one-off override for one of the group's declared inputs. Applies only to the next `.run()`. |
| `.run()` | Blocking; runs whatever feeds the group's declared outputs (`kpl group run` under the hood) plus their upstream dependencies; nodes merely jump-linked *after* the output nodes do not run. Returns `True`/`False`. |
| `.outputs['port']` | The group's output value from its last run (from cache). |
| `.remove_cache()` | Blocking; clears cache for every member node (`kpl group clean` under the hood; frozen members are skipped). Returns `True`/`False`. |

Accessing any of these on a slot that isn't bound in this pipeline raises
`RuntimeError` naming the slot, not a bare `KeyError`.

If a slot's `address` (category/name of a group exported as a reusable
component from the GUI — a `<groups_dir>/<category>/<name>.yaml`
(the pipeline's `groups_dir`, else the installation's `group_base_nodes`) template, see the User Guide's Groups section) resolves when a node is
created, the GUI auto-instantiates and binds that component immediately; if
it doesn't resolve, the GUI asks whether to create the node anyway (slot
left unbound) or cancel. No `address` → the slot is just created empty, to
bind manually later. This binding/auto-instantiation only happens in the
GUI — creating a node via `kpl add` always leaves every slot unbound.

### 3.7 Underlying nodes — `self.nodes[...]`

Same idea as §3.6, but for ONE node instead of a group: the node decides
**if, when, how often and with which inputs** another node runs. A node type
declares named slots (`underlying_nodes` in `node_config.yaml`, shown in §3.1);
each node instance binds a slot to a node of the same pipeline through
`graph_config.yaml` (on the node, mirroring `groups:`):

```yaml
- name: loadcell_check_1
  type: loadcell_check
  underlying_nodes:
  - {name: table, value: table_ctrl_3}     # slot name -> node name in this pipeline
```

(In the GUI, the node's Properties panel has an **Underlying Nodes** section with
one dropdown per slot — filtered to the slot's declared `type`. Like a bound
group, a bound node **looks disabled** (hatched, inert) until it is made active:
click its chip under the owner node (one active bound node per owner), or
double-click it. Clicking an inactive bound node highlights its owner (without selecting it). "Active" is a
transient focus — clicking the empty canvas or an unrelated node deactivates it —
and purely visual: it is not saved and does not change how anything runs. The
binding follows a rename of the bound node and is cleared if that node is
deleted.)

The bound node is deliberately **not** a `nodes.X.Y` data dependency of the
owner. The scheduler never runs it on its own — it runs only when the owner's
code asks:

```python
def run(self):
    table = self.nodes['table']
    for angle in (0, 10, 20):
        table.inputs['angle'] = angle          # one-off override — NOT saved to graph_config.yaml
        if not table.run():                    # blocking; True/False
            sys.exit(1)
        weight = table.outputs['weight']       # read after a successful run (from cache)
```

| Member | Meaning |
|---|---|
| `.inputs['port'] = value` | Stage a one-off override for one of the bound node's declared inputs. Applies only to the next `.run()`. |
| `.run(use_cache=True)` | Blocking; runs the bound node **every time it is called** (`kpl run` under the hood). `use_cache` only controls whether the bound node's *own upstream dependencies* may come from cache. Its jump-link successors are **not** run. Returns `True`/`False` (a failed node → `False`). |
| `.outputs['port']` | The bound node's output value from its last run (from cache). |
| `.remove_cache()` | Blocking; clears the bound node's cache (`kpl clean`; a frozen node is skipped). Returns `True`/`False`. |
| `.node_id` / `.node_type` | Name / type of the bound node (`None` if unbound). |

Accessing `.run()`, `.inputs` or `.outputs` on an unbound slot raises
`RuntimeError` naming the slot. A node that would run itself, directly or
through a chain (`A → B → A`), is refused with a `RuntimeError` instead of
nesting forever.

Limits (same as `self.groups`): the nested node's state and output are not
shown live in the terminal/GUI (only in its own `log/run.log`), it bypasses the
executor's cpu/gpu worker limits, and a nested node with `kwui: true` gets no
real UI.

CLI building blocks (opt-in, default behavior of `kpl run`/`kpl clean`
unchanged): `kpl run --no-jump-forward <node>` skips the node's jump
successors, `--exit-status` makes `kpl run`/`kpl clean` exit 1 on failure
(otherwise they always exit 0). Flags must come **before** `<node>` —
`kpl run` treats everything after the node name as node parameters.

---

## 4. Hard rules checklist

Before finishing, verify:

- [ ] All node / input / output names match `\w+`.
- [ ] Every output declared in `node_config.yaml` is written with `save_output()` —
      a missing one silently gives `None` downstream.
- [ ] Result files are written under `self.output_dir` only (that is the cache dir;
      writing elsewhere breaks caching and download-from-GUI).
- [ ] `node.py` ends with `KapNode(arg_parser=True)` and the class subclasses
      `KNode` with a `run()` method.
- [ ] References use exactly `nodes.<name>.<output>` and the referenced node +
      output exist.
- [ ] An input with an empty-string value is NOT passed on the command line — the
      node then sees its `node_config.yaml` default.
- [ ] BOOL inputs: use a real YAML `true`/`false` (not a quoted `'true'` string) as
      the value — a quoted string still round-trips through argparse correctly,
      but only the unquoted boolean form is idiomatic. In a direct CLI test they
      are `--verbose` / `--no-verbose`, not `--verbose=true`.
- [ ] Long-running loops report progress via `send_status_message` (else the user
      sees nothing during the run).
- [ ] Don't hand-edit anything under `cache/`; use `kpl clean` to invalidate.
- [ ] After changing a node's code, re-run with cache cleared for that node
      (`kpl clean <node>` or `kpl run <node>` and confirm the cache question in
      the GUI) — otherwise the old cached result is reused.
- [ ] Max 500 nodes per graph.

---

## 5. Minimal end-to-end example

Project-local node source + pipeline, from nothing:

```bash
mkdir -p proj/nodes/math/make_number proj/nodes/math/add_offset proj/pipe
```

`proj/nodes/math/make_number/node_config.yaml`:

```yaml
description: "Emits a number."
category: "math"
inputs:
  - name: value
    type: INT
    default: 41
outputs:
  - name: number
    type: INT
```

`proj/nodes/math/make_number/node.py`:

```python
import os, shutil, sys
sys.path.append(os.path.dirname(shutil.which("kpl") or shutil.which("kpipeliner")))
from kpipeline import KNode

class KapNode(KNode):
    def run(self):
        self.save_output("number", int(self.inputs["value"].value))
        self.send_status_message("COMPLETED", message="done")

if __name__ == "__main__":
    KapNode(arg_parser=True)
```

`add_offset` is identical in structure: inputs `number` (INT, default 0) and
`offset` (INT, default 1), one output `sum`, and in `run()`:
`self.save_output("sum", int(self.inputs["number"].value) + int(self.inputs["offset"].value))`.

```bash
cd proj/pipe && kpl init
```

Set `proj/pipe/graph_config.yaml` nodes + sources:

```yaml
custom_base_nodes_dirs: ["../nodes"]
nodes:
  - name: make_number_1
    type: make_number
    inputs: [{name: value, value: 41}]
    outputs: [{name: number}]
  - name: add_offset_1
    type: add_offset
    inputs:
      - {name: number, value: nodes.make_number_1.number}
      - {name: offset, value: 1}
    outputs: [{name: sum}]
```

```bash
kpl list          # both types appear under [math]
kpl run           # runs make_number_1 → add_offset_1
cat cache/$(python3 -c "import yaml;print(yaml.safe_load(open('extern/uuid.yaml'))['value'])")/add_offset_1/outputs.yaml
# → sum: 42
```

- Custom base nodes can be created as one directory in your project (e.g. custom_base_nodes)
- you can put nodes under different category if needed. 
- all nodes in one category can have a share python environemnt as well (if needed).

---

## 6. MCP interface (optional — faster than the CLI for an agent)

This needs the separate, optional `mcp` Python package (kpipeliner's `mcp` extra) — the
base install deliberately excludes it (most installations never need it).
If `kpl mcp`/`kpl gui --mcp` print a message about it being missing, install
with `uv pip install "kpipeliner[mcp]"` (or `pip install "kpipeliner[mcp]"`) —
or just use the CLI workflow in §2/§3 instead, nothing here requires MCP.

If this pipeline's kpipeliner installation is running with `kpl mcp` (headless,
no GUI/REST API at all — just the MCP endpoint) or `kpl gui --mcp` (GUI + MCP
together, same port) and has been registered with your MCP client (e.g. for
Claude Code: `claude mcp add --transport http kpipeliner
http://localhost:<port>/mcp`), the tools below are available directly — named
`mcp__<server-name>__<tool>`, where `<server-name>` is whatever name it was
registered under (e.g. `kpipeliner`). Prefer them over the CLI workflow in §2/§3
when available: structured JSON in and out, no output-parsing, and every edit
still lands in the exact same `graph_config.yaml` / node-type files described
above — nothing about the format changes, only how you drive it.

| Tool | Same as (CLI) | Notes |
|---|---|---|
| `init_pipeline` | `kpl init` | |
| `list_node_types` | `kpl list` | structured: type, category, inputs, outputs, description |
| `get_graph` | `kpl tree` / reading `graph_config.yaml` | full current graph as JSON |
| `add_node` | `kpl add <type> [name]` | |
| `remove_node` | *(no CLI equivalent)* | also clears dangling `nodes.<name>.*` references left in other nodes |
| `set_node_input` | hand-editing an input's `value` in `graph_config.yaml` | literal value, or `nodes.<name>.<output>` to connect it |
| `set_pipeline_io` | hand-editing top-level `inputs:`/`outputs:` | `outputs` still scopes a no-argument run exactly as in §2 |
| `run_pipeline` | `kpl run [node]` / `kpl run_debug [node]` | blocks until finished; returns combined stdout/stderr + exit code |
| `get_status` | `kpl status` | |
| `get_tree` | `kpl tree` | |
| `create_node_type` | `mkdir` + copying the §3.1/§3.2 templates | writes into this pipeline's own `custom_base_nodes_dirs` only (creates one, `./nodes`, if none is configured yet) |
| `read_node_file` | `cat node_config.yaml` / `node.py` | works for any node type visible to the pipeline, including shared/default ones |
| `write_node_file` | editing `node_config.yaml` / `node.py` by hand | **pipeline-local node types only** — refuses shared/default node types by design (other pipelines use them too); run `create_node_type` first to get a local copy you can edit |

**Not connected yet?** Start the server from the pipeline directory, then hand
the user the one-time registration command for their client — don't wait around
for it to appear mid-task; fall back to the CLI workflow in §2/§3 for the
current request instead, since a newly registered server only becomes visible
after the client reconnects (e.g. Claude Code's `/mcp`, or a new session):

```bash
kpl mcp             # headless, no GUI/REST API at all — just the MCP endpoint
kpl gui --mcp       # or with the GUI open too — same port either way
# prints e.g.: kpipeliner MCP → http://localhost:8601/mcp
```

Tell the user (for Claude Code): `claude mcp add --transport http kpipeliner
http://localhost:<printed-port>/mcp`.

---

## 7. kwui mode — a web UI per node (optional)

Requires the `kwebui` package in kpipeliner's Python environment. A kwui run is
the same pipeline run as always; the only difference is that it happens behind a
kwebui page (own port) showing a step tracker plus one container that always
displays whichever node is currently running.

### 7.1 Set it up (once per pipeline)

```bash
kpl kwui init            # copies workflow.py into the pipeline directory
kpl run --kwui           # or: kpl run_debug --kwui
```

The UI listens on all interfaces (default `0.0.0.0:8701`, first free port at or
above it) and a browser is opened once it is up; both the local and the LAN URL
are printed. Defaults come from `config.yaml`'s `kwui:` section
(`host` / `port` / `open_browser`), per-run overrides are `--kwui-host`,
`--kwui-port`, `--kwui-no-browser`. kwebui has no authentication — use
`--kwui-host 127.0.0.1` to keep a run private to the machine.

`workflow.py` is yours to edit. `kpl` only requires that its `KApp` subclass sets
two attributes in `build()`:

- `self.tracker` — a `workflow_tracker` whose task **ids are node names**
- `self.app_container` — the `container` each running node's `self.ui` writes into

Optional: a class attribute `history` on the `KApp` subclass — `True` (the default
when it is left out) lets the user click a finished step in the tracker to see what
that node displayed; `False` shows only the running step, as before. The template
from `kpl kwui init` also has **Refresh page**, **Restart step**, **Continue** and **Reset all** buttons; see 7.3.

`load_tasks()` (from `kpipeline.src.kwui`) fills the tracker from the pipeline's
**jump-link sequence**: it starts at every node listed in `graph_config.yaml`'s
top-level `inputs:` and follows each node's jump-out until one has none. A
jump-out feeds at most one target, so this is always a linear sequence — that's
what makes a single "current step" meaningful. Only the nodes of that sequence
that are **kwui-enabled** (`kwui: true` in `node_config.yaml`, see 7.2) become
tracker steps; the other nodes in the chain still run, in order, exactly as in a
normal run — they just have no UI, so the workflow doesn't list them.

**Input references are resolved exactly as in `kpl run <node>`**: before a step
runs, every `nodes.X.Y` input is either read from the cache or its producing node
is run recursively first (kwui-enabled or not, in the chain or not) — kwui mode
goes through the same `run_node()` path, so a UI node always starts with its
inputs ready. Such a dependency deliberately gets no tracker step of its own;
while it runs, the container shows the "Working ..." spinner.

### 7.2 Make a node kwui-enabled

`node_config.yaml`:

```yaml
kwui: true
```

Then `self.ui` inside `run()` is a live handle to `app_container`; without
`kwui: true` it is `None`. **Every widget kwebui registers works**, with its
full constructor signature — the bridge forwards by name against kwebui's live
registry, so a widget (or a new argument on an existing one) added by a kwebui
upgrade is usable immediately, with no change here:

`text`, `button`, `checkbox`, `textedit`, `slider`, `listbox`, `progressbar`
(alias `progress`), `spinner`, `image`, `imagestream`, `file_uploader`, `html`,
`json`, `table`, `empty`, `container`, `success`/`info`/`warning`/`error`,
`badge`, `toast`, `columns`/`column`, `sidebar`, `topbar`, `popup`,
`workflow_tracker`.

Each returns a handle whose methods forward to the real widget — both the
common ones (`update(...)`, `highlight(color)`, `unhighlight()`, `hide()`,
`show()`, `disable()`, `enable()`, `remove()`, `focus()`) and each widget's own
(`set_text`, `set_value`, `set_checked`, `set_data`, `set_task_status`,
`push_frame`, ...).

**Layout containers nest.** A handle for a container-like widget (`container`,
`empty`, `sidebar`, `topbar`, `column`) creates children on itself, and the
child handle stays addressable afterward:

```python
box = self.ui.container(caption="Result", direction="horizontal")
label = box.text("computing ...")      # child handle, not the container
box.button("Cancel", on_click=self.cancel)
label.set_text("done")                 # still addressable later

left, right = self.ui.columns(2)       # also: cols = self.ui.columns([0.3, 0.7]); cols[0]
left.image("/path/to/plot.png", width=320)
right.table(df)                        # a pandas-like DataFrame works directly
```

**Binary data works both ways**: `imagestream.push_frame(jpeg_bytes)` down to
the browser, and `file_uploader(on_upload=...)`'s `(filename, data: bytes)`
back up into the node — both base64-encoded across the process boundary for
you.

**`with` blocks** call the real widget's own context manager, so
`with self.ui.spinner("Working...", show_time=True):` really does tick its
elapsed timer (and is a harmless no-op on widget types that have none).

**App-level calls** go to the kwebui app itself rather than the container:
`self.ui.set_theme("dark")`, `self.ui.on_shutdown(cb)`, `self.ui.exit()`.

In the GUI, a kwui node type is marked twice over: a **cyan ring** plus visibly
**rounder corners** on the canvas node, and a cyan `UI` badge in its title bar
and in the components palette. It's derived from the node type's
`node_config.yaml` (exposed as `is_kwui` per node by `/api/graph`, `kwui` per
type by `/api/nodes`) and is never written back into `graph_config.yaml`.

**Callbacks stay in node.py.** `on_click`/`on_change`/`on_select` receive the
real Python callable defined in your node — the browser event is relayed back
into the node process and invoked there, in a separate thread.

```python
class KapNode(KNode):
    def run(self):
        self.ui.text("Review the result", size=24, bold=True)
        self.status = self.ui.text("waiting...")
        self.ui.button("Approve", on_click=self.approve)
        self.ui.button("Try again", on_click=lambda: self.ui.end("REPEAT"))

        outcome = self.ui.interact()          # blocks until end() is called
        self.save_output("verdict", outcome)

    def approve(self):
        self.status.set_text("approved")      # mutates the live widget
        self.ui.end("OKAY")
```

| Member | Meaning |
|---|---|
| `self.ui.<widget>(...)` | Create a widget in the workflow container; returns a forwarding handle. |
| `<handle>.<widget>(...)` | Create a CHILD widget inside a container-like handle; returns the child's own handle. |
| `<handle>[i]` | One column of a `columns` handle (also `len()` / iteration). |
| `self.ui.clear()` | Wipe everything this node put in the container. |
| `self.ui.load_layout(path=None, handlers=None)` | Show a page designed visually (`kpl kwui design <node_type>`, see 7.5) and return `{widget id: handle}`; handler names in the layout call methods of this node. |
| `self.ui.interact(timeout=None)` | Block until `end()` is called; returns exactly the `msg` it was given, unchanged (`None` on timeout). |
| `self.ui.end(msg)` | Releases `interact()`, which returns `msg`. `OKAY` (success) / `ERROR` (fail the node even on exit 0 — the step icon shows an error and the workflow STOPS and waits) / `ERROR_SKIP` (the step icon shows an error but the workflow CONTINUES with the next step; the node counts as done, so outputs it saved stay usable and it is not re-run) / `REPEAT` (re-execute the node) are matched case-insensitively; any other message (str, dict, number...) counts as success and is just data for `interact()`'s caller. |
| `self.ui.end("JUMP", "node")` / `self.ui.jump_to("node")` | Finish this node and continue the workflow at `node` — earlier (redo from there) or later (skip ahead). See 7.3. `end("JUMP")` without a target is just a data message. |
| `self.ui.set_theme(name)` / `.on_shutdown(cb)` / `.exit()` | The kwebui app's own methods, not widgets. |

### 7.3 Moving around the workflow — history, JUMP, Refresh, Reset

**History.** With `history = True` (default) every tracker step keeps its own page.
Clicking a step that has started (running, finished or failed) in the tracker shows
what that node last displayed; the running node keeps writing to its own page even
while you look at an old one. Widgets on old pages are disabled, and a click from an
old page can never reach the node running now. A node that runs again (REPEAT, jump,
Refresh) gets a fresh page. Data-dependency helper nodes (no tracker step) share one
"Working ..." page. Only for the plain kwebui `tracker`/`container` that the template
creates — a `workflow.py` that supplies its own stand-in objects for them is left
alone and behaves exactly as before. `history = False` restores the old behavior.

**JUMP.** From node code:

```python
self.ui.jump_to("calibrate")          # same as self.ui.end("JUMP", "calibrate")
```

The node ends successfully and the workflow continues at `calibrate`, which must be a
node of the workflow sequence (the jump-link chain, UI step or not). Going **back**
re-runs that step and every later one; going **forward** skips the steps in between
(they stay pending and are not run, unless a later step needs one of their outputs via
`nodes.X.Y`). A name that is not in the sequence fails the node with a message listing
the valid names. `jump_to(<this node>)` behaves like `REPEAT`. After asking for a jump a node may keep
running for **5 seconds** (to save outputs, close devices); if its process is still
alive then, it and everything it started are stopped and the workflow moves on
(otherwise a node whose process never exits would hold the workflow on its step
forever). A plain `end("OKAY")` is NOT cut short: the node is waited for as always,
since many nodes keep working after `interact()` returns. Steps from the target
onward are reset in the tracker and their old pages dropped; helper nodes that
compute from a re-run step are recomputed too (cached results of nodes that depend on
a re-run step are not reused until they have run again — nothing is deleted from
disk).

**The template's buttons:**

| Button | Effect |
|---|---|
| **Refresh page** | Reloads the browser page, exactly like F5. Purely client-side (`onclick="location.reload()"` in an `html` widget): it does not touch the running node, and the page, tracker and history are simply drawn again. |
| **Restart step** | `restart_step`: stops the running node and runs the CURRENT step again with a freshly built page; earlier steps are untouched. After an error it is the step that failed; after the workflow ended, the last step. |
| **Continue** | `continue_workflow`: only when the workflow STOPPED because of an error — goes on with the next step; the failed step stays shown as an error and counts as done (a later step reading its outputs does not start it again). Otherwise it does nothing and the terminal says so. |
| **Reset all** | `reset_all`: stops the running node and starts the whole workflow over from its first step. |

These functions (all from `kpipeline.src.kwui`) and `jump_to_step("node")` (continue at a
given step) can also be wired to buttons of your own. Stopping covers **every** node of the run — a UI step, a non-UI
step or a data-dependency helper alike: each node run's processes carry a unique
marker (`KPL_KWUI_RUN_TOKEN`) in their environment, so the process tree AND anything
the node started that detached or was re-parented are found, sent SIGTERM and, if still
there after 2 s, SIGKILL; the next step starts only after they are gone (a message
lists anything that could not be stopped). A node in the middle of something — a motor
move, a file write — is cut off, like with CTRL+C. A child that is daemonized AND
starts with a scrubbed environment cannot be recognized and is not stopped.
**Errors: stop or carry on.** `self.ui.end("ERROR")` marks the step as an error and stops
the workflow there (Reset all starts over). `self.ui.end("ERROR_SKIP")` marks the step as
an error too, but the workflow goes on with the next step. For the pipeline the node is
*done* — save any outputs before calling it; a later step that reads them gets them, and
the node is not started again. The terminal says which step ended that way and, when the
workflow finishes, lists them. If the node then exits with an error code (a crash after
`end("ERROR_SKIP")`), that is a real failure and the workflow stops.

The second argument of `end()` is a short **text shown under the step icon** for `ERROR` /
`ERROR_SKIP`: `self.ui.end("ERROR_SKIP", "camera not found")` (or `detail="..."`); it is cleared
when the step runs again or on Reset. (For `JUMP` the second argument is the target node.)

**Crashes.** A node that crashes without calling `end()` stops the workflow like `ERROR` — Continue
then goes on, Restart step retries. A node TYPE can decide that its crash should not block:
`on_error: skip` in its `node_config.yaml` treats a crash like `ERROR_SKIP` (shown on the step, with
the text "crashed (exit code N)", workflow continues). An explicit `end("ERROR")` still stops.

**What stays on screen:** when a node is stopped (Reset, a jump, a failure) its widgets
are dropped with its page, and any timed spinner it left (`spinner(show_time=True)`
ticks its seconds on the server until the node's `with` block exits) is ended first —
otherwise it would keep counting on the page as a stray widget.

**Leftovers:** a node that finished but left a helper running (a service it started) does
not hold the workflow — once the node's own process has exited, kpl stops waiting for
the helper's inherited output pipe after about a second (kwui runs only; a plain
`kpl run` is unchanged). Such a helper keeps running until the step runs AGAIN (Reset
all, a jump back): then everything the earlier run of that step left behind is
stopped first, so copies never pile up.
After the last step, or after a failure, the workflow stays open and waits: Reset all
starts over. (`refresh_step` from an earlier template version still imports but only
prints a hint — a refresh is a browser reload now.)

### 7.4 Rules and current limits

- A node that does **not** call `interact()` just runs to completion, as always —
  nothing in kwui mode blocks by itself.
- A non-kwui node shows a "Working ..." spinner in the container while it runs.
- kwui runs are **sequential** (the parallel executor is not used), so a single
  "current step" is unambiguous.
- The same node.py works in a plain `kpl run`: with no UI attached, `self.ui`
  becomes a no-op sink and `interact()` returns `OKAY` immediately.
- Widget **method return values do not cross back** — handles are
  fire-and-forget, so a read-back method (`imagestream.latest_frame()`,
  `checkbox.checked`, `KApp.session`) isn't usable through `self.ui`. Track the
  value in your own node code instead: the callbacks (`on_change` etc.) already
  deliver every change into the node process.
- For the same reason, `imagestream`'s **pull** model (`frame_provider=`, which
  requires kwebui to receive a return value from a node-side callable) is not
  supported — use the **push** model, which is: call
  `stream.push_frame(jpeg_bytes)` from a background thread in the node.
- Widget and callback arguments must be JSON-serializable, with three
  exceptions handled for you: `bytes`/`bytearray` (both directions), a
  pandas-like DataFrame (converted to the row shape `table` accepts), and
  callables (relayed as callbacks). Anything else (numpy arrays, PIL images,
  `datetime`, sets) must be converted in the node first — same as kwebui
  itself requires, since it does no encoding of its own. For images that means
  writing a file and passing its path, or JPEG-encoding to bytes for
  `imagestream`.
- The pipeline starts once the first browser connects (max 120s wait), and the
  server keeps serving after it finishes so the final state stays visible —
  `kpl run --kwui` returns on CTRL+C. A run left open therefore keeps port 8701:
  the next run warns and moves to 8702, so always open the freshly printed URL
  instead of an old tab. CTRL+C frees the port (and stops a still-running node);
  the next run gets the same port again immediately.

### 7.5 Designing a node's page visually (kwebui edit mode)

Instead of writing `self.ui.text(...)`, `self.ui.button(...)` ... in code, a node's page can be
**laid out in the browser** and loaded by the node. Needs `kwebui >= 1.0.0` (`kpipeliner[kwui]`).

```bash
kpl kwui design <node_type>                 # edits <node dir>/ui.layout.json
kpl kwui design --layout path/to/x.json     # any other layout file (created on the first change)
kpl kwui design --show-code --node <name>   # ALSO runs that node, to show the UI its code draws (see below)
```

The layout file is `ui.layout.json` **in the node type's own folder**, beside `node.py` and `node_config.yaml` (the folder is
found through the pipeline's base-node dirs, like `kpl add`); it is created on the first change.

The editor opens in the browser (`--host/--port/--no-browser` like `--kwui-*`; defaults from
`config.yaml`'s `kwui:` section). Right-click inside a container to insert a widget, click a widget
to edit its properties, drag to move/reorder, drag the gap between columns to resize. Every change
is saved to the file at once. In the **GUI**, right-click a `UI` node → **Edit UI** opens the same
editor in a new tab (only offered when the server's Python environment has a kwebui with edit mode).

In the editor, give each widget the **id** your code will use, and for callbacks type a **method
name** in the "Handlers" field (e.g. `on_click` → `on_save`). In the node:

```python
class KapNode(KNode):
    def run(self):
        self.page = self.ui.load_layout()             # <node dir>/ui.layout.json (a relative path is relative to the node dir)
        self.page["title"].update(text=f"Camera {self.inputs['cam'].value}")
        self.page["cols"][1].text("added to the second column")   # columns by position (or by their own layout ids)
        self.ui.interact()

    def on_save(self):                                 # named in the layout's Handlers field
        self.page["status"].update(text="saved")
        self.ui.end("OKAY")
```

Rules:
- `load_layout(path=None, handlers=None)`: the returned dict maps each layout id to the same handle
  `self.ui.<widget>(...)` returns (`update`, `set_text`, child widgets, ...). `handlers=` is the object whose
  methods the Handlers field names (default: the node). Plain `self.ui.<widget>(...)` calls may follow; they land below the layout.
- Strict, like kwebui: a missing file, a widget type kwebui doesn't know, or a handler name the node
  doesn't have raises when `load_layout` runs — in a plain `kpl run` too, not only in kwui mode.
- Every load gets its own widget-id prefix inside the browser, so two nodes (or two runs on history
  pages) may use the same ids. Your code keeps using the ids from the editor.
- The layout's **page settings** (width, theme) only matter while designing: at run time the workflow
  page (`workflow.py`) owns them.
- Without a live UI (plain `kpl run`) `load_layout` — and every widget call — returns no-op handles
  (`update(...)`, `cols[0].text(...)` work and do nothing), so the same node.py works in both modes.
- The designed page is for the node's own page; the workflow page (sidebar, tracker, buttons) stays `workflow.py`.

**Showing the coded UI in the editor (`--show-code --node NAME`).** A page built with `self.ui.text(...)` etc. exists only
while the node runs, so the editor cannot show it by itself. With `--show-code` the designer also **runs the node
instance `NAME`** of the current pipeline behind the editor, exactly as a kwui run would, and the widgets its code draws
appear **dimmed and not editable** (kwebui edit mode: only widgets that come from the layout file are editable). Rules:
this executes the real node code (hardware included) up to its `interact()`; **no other node is run** — inputs that
reference other nodes use their cached outputs, else stay unset (a message says so); `load_layout()` calls inside the node are skipped
(the editor already shows that layout, editable, and the coded widgets are listed above it, not in their real order);
the node ends when the designer stops. This is a **CLI-only** option: the GUI's **Edit UI** menu entry never runs a node (a
GUI entry for it existed briefly and was removed again at the user's request).- kwebui 1.0 serves ONE browser tab at a time: opening the page in a second tab hands the session to that tab
  and the first shows "superseded" with a Reload button (this also applies to `kpl run --kwui` over the LAN).
