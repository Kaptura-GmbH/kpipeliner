"""
kpl (kpipeliner) node template.
Copy this file to your node directory as node.py.
"""
import os
import shutil
import sys

# If kpl (kpipeliner) is installed, this import will work directly.
sys.path.append(os.path.dirname(shutil.which("kpl") or shutil.which("kpipeliner")))

from kpipeline import KNode


class KapNode(KNode):

    def run(self):
        """
        Main node logic. Called automatically when the node is executed.

        Useful attributes:
            self.inputs['name'].value   – get input value (read lazily from the
                                          source node's cache the first time you
                                          access it, if this input is wired to
                                          another node via nodes.X.Y)
            self.inputs['name'].source_node   – set only when this input came from
                                          a nodes.X.Y reference (None for a literal
                                          value). Identity only:
                self.inputs['name'].source_node.name   – the source node's id
            self.output_dir             – directory for output files
            self.data_dir               – shared data directory
            self.uuid                   – pipeline run UUID
            self.name                   – node instance name

        Useful methods:
            self.log("message")                          – info log
            self.log("message", level="debug")           – debug log (hidden unless
                                                             graph_config.yaml's log_level: DEBUG)
            self.log("message", level="warning")         – warning log
            self.log("message", level="error")           – error log
            self.send_status_message("RUNNING", progress=50, max=100, message="halfway")
            self.save_output("output_name", value)       – save and broadcast output
            self.make_dir("subdir")                      – create subdirectory in output_dir

        With `kwui: true` in node_config.yaml, self.ui is this node's panel in the
        `kpl run --kwui` workflow UI (None otherwise):
            self.ui.text("hello"), self.ui.button("Go", on_click=self.my_method)
            msg = self.ui.interact()       – block until self.ui.end(msg) is called; returns that msg
            self.ui.end("OKAY" | "ERROR" | "REPEAT" | any other message/object)

        Every self.log() call (and any plain print(), logged like an info
        call) also goes to a per-run file at output_dir/log/run.log, appended
        across runs. Verbosity is controlled pipeline-wide by graph_config.yaml's
        log_level (default INFO — debug is hidden unless set), not per node;
        log_enabled: false in graph_config.yaml turns file logging off entirely.
        """

        # Read inputs
        my_input = self.inputs['input_param'].value
        self.log(f"Input: {my_input}")

        # Send progress
        self.send_status_message("RUNNING", progress=0, max=100, message="Starting...")

        # --- do your work here ---

        self.send_status_message("RUNNING", progress=100, max=100, message="Done.")

        # Save outputs
        self.save_output("result", "output_value_here")

        self.send_status_message("COMPLETED", message="Node finished.")


if __name__ == "__main__":
    KapNode(arg_parser=True)
