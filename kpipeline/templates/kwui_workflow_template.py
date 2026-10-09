"""Workflow UI for this pipeline — served by `kpl run --kwui`.

`load_tasks()` reads graph_config.yaml and returns one tracker step per UI node
(`kwui: true`) in the pipeline's jump-link sequence (starting at the declared
`inputs:` nodes, following each node's jump-out until one has none). Nodes in
that sequence without a UI still run, but are not listed.

`kpl` looks for exactly two attributes on this class:
  * `self.tracker`       — a workflow_tracker whose task ids are node names
  * `self.app_container` — the container each running node's `self.ui` writes into

Everything else here is yours to change. Run it only through
`kpl run --kwui` / `kpl run_debug --kwui`, which put kpipeliner on sys.path.
"""

from kwebui import KApp

from kpipeline.src.kwui import load_tasks

TASKS = load_tasks()


class Workflow(KApp):
    def build(self) -> None:
        sidebar = self.sidebar()
        sidebar.text("Workflow", size=18, bold=True)
        self.tracker = sidebar.workflow_tracker(list(TASKS), orientation="vertical")

        self.app_container = self.container()
