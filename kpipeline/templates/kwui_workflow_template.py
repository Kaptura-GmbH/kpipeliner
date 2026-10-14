"""Workflow UI for this pipeline — served by `kpl run --kwui`.

`load_tasks()` reads graph_config.yaml and returns one tracker step per UI node
(`kwui: true`) in the pipeline's jump-link sequence (starting at the declared
`inputs:` nodes, following each node's jump-out until one has none). Nodes in
that sequence without a UI still run, but are not listed.

`kpl` looks for exactly two attributes on this class:
  * `self.tracker`       — a workflow_tracker whose task ids are node names
  * `self.app_container` — the container each running node's `self.ui` writes into

Optional class attribute:
  * `history`  — True (the default if you leave it out): clicking a finished step
                 in the tracker shows what that node displayed. False: only the
                 running step is shown, as in older versions of this file.

The buttons:
  * Refresh page — reloads the browser page, exactly like pressing F5. It does not
                   touch the running node; the page is simply drawn again.
  * Restart step — stops the running node (and everything it started) and runs the
                   current step again with a fresh page. After an error it retries
                   the step that failed.
  * Continue     — only when the workflow stopped because of an error: carries on
                   with the next step (the failed step stays shown as an error).
  * Reset all    — stops the running node (and everything it started) and starts the
                   whole workflow over from its first step.

A node can also send the workflow to another step itself:
`self.ui.jump_to("node_name")` (same as `self.ui.end("JUMP", "node_name")`), and choose
what an error does: `self.ui.end("ERROR")` stops the workflow at the step,
`self.ui.end("ERROR_SKIP", "optional text")` shows the error on the step and goes on.

Everything else here is yours to change. Run it only through
`kpl run --kwui` / `kpl run_debug --kwui`, which put kpipeliner on sys.path.
"""

from kwebui import KApp

from kpipeline.src.kwui import continue_workflow, load_tasks, reset_all, restart_step

TASKS = load_tasks()


class Workflow(KApp):
    # Let the user click finished steps to look at what each one showed.
    history = True

    def build(self) -> None:
        sidebar = self.sidebar()
        sidebar.text("Workflow", size=18, bold=True)
        self.tracker = sidebar.workflow_tracker(list(TASKS), orientation="vertical")
        # Reload the browser page (like F5) — done in the browser itself, so it works
        # even while the server is busy.
        sidebar.html('<button type="button" class="sg-button" onclick="location.reload()">Refresh page</button>')
        sidebar.button("Restart step", on_click=restart_step)
        sidebar.button("Continue", on_click=continue_workflow)
        sidebar.button("Reset all", on_click=reset_all)

        self.app_container = self.container()
