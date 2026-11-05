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

The buttons (at the top of the sidebar, above the step list):
  * Restart step — stops the running node (and everything it started) and runs the
                   current step again with a fresh page. After an error it retries
                   the step that failed. If you clicked an earlier step in the
                   tracker to look at its output, THAT step is run again (and the
                   steps after it).
  * Skip step    — goes on with the next step. If a step is running, it is stopped
                   first (with everything it started); it stays in the list, marked
                   "skipped", and you can still click it to see what it showed. After
                   an error it carries on past the failed step.
  * Refresh page — reloads the browser page, exactly like pressing F5. It does not
                   touch the running node; the page is simply drawn again.
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

from kpipeline.src.kwui import load_tasks, reset_all, restart_step, skip_step

TASKS = load_tasks()

# Compact sidebar buttons: smaller than kwebui's default and each filling its half of
# the row. Scoped to the sidebar, so buttons in your node pages keep the normal look.
_SIDEBAR_STYLE = """<style>
.sg-sidebar .sg-columns { margin-bottom: 0.6rem; }
.sg-sidebar .sg-column { gap: 0.4rem; }
.sg-sidebar .sg-button { width: 100%; padding: 0.3rem 0.5rem; font-size: 0.82rem;
                         border-radius: 5px; white-space: nowrap; }
.kpl-btn-neutral { background: var(--sg-bg); color: var(--sg-fg); }
</style>"""

# Button colors: theme accent = run again, amber = go past, red = start over,
# neutral = browser-only.
SKIP_COLOR = "#d97706"
RESET_COLOR = "#dc2626"


class Workflow(KApp):
    # Let the user click finished steps to look at what each one showed.
    history = True

    def build(self) -> None:
        sidebar = self.sidebar()
        sidebar.text("Workflow", size=18, bold=True)
        sidebar.html(_SIDEBAR_STYLE)
        left, right = sidebar.columns(2, gap="0.4rem")
        left.button("Restart step", on_click=restart_step)
        right.button("Skip step", on_click=skip_step, color=SKIP_COLOR, text_color="white")
        # Reload the browser page (like F5) — done in the browser itself, so it works
        # even while the server is busy.
        left.html('<button type="button" class="sg-button kpl-btn-neutral" '
                  'onclick="location.reload()">Refresh page</button>')
        right.button("Reset all", on_click=reset_all, color=RESET_COLOR, text_color="white")
        self.tracker = sidebar.workflow_tracker(list(TASKS), orientation="vertical")

        self.app_container = self.container()
