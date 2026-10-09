"""
Parallel pipeline executor.

Runs independent nodes concurrently up to the configured CPU/GPU worker limits.
On any node failure the pipeline is halted and dependent nodes are marked SKIPPED.
"""
import threading
import time

from kpipeline.src.knode import NodeState


class PipelineExecutor:

    def __init__(self, graph, cpu_workers=2, gpu_workers=1,
                 debug=False, on_state_change=None, line_callback=None):
        """
        graph           – KGraph instance.
        cpu_workers     – max simultaneous CPU-type nodes.
        gpu_workers     – max simultaneous GPU-type nodes (detected via node_config).
        debug           – pass debug=True to every node execute().
        on_state_change – callback(node_name, state, error=None).
        line_callback   – callback(node_name, line) for raw stdout lines.
        """
        self.graph = graph
        self.cpu_workers = max(1, cpu_workers)
        self.gpu_workers = max(1, gpu_workers)
        self.debug = debug
        self.on_state_change = on_state_change
        self.line_callback = line_callback

        self._states = {}
        self._executed = set()
        self._failed = set()
        self._running = set()
        self._pending = set()
        self._error_info = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self, start_node=None, use_cache=False, pipeliner=None):
        """
        Execute the pipeline.

        start_node – if given, run only that node and its transitive dependencies.
        Returns True if all nodes succeeded, False otherwise.
        """
        if start_node:
            nodes_to_run = self.graph.get_all_required(start_node)
        else:
            # No specific node requested: run only what's needed to produce
            # the pipeline's declared outputs (graph_config.yaml `outputs:`),
            # not the entire graph. Empty outputs -> nothing to run.
            nodes_to_run = self.graph.get_run_order()

        with self._lock:
            self._pending = set(nodes_to_run)
            self._states = {name: NodeState.PENDING for name in nodes_to_run}
            self._executed.clear()
            self._failed.clear()
            self._running.clear()
            self._error_info.clear()
            self._stop.clear()

        if self.on_state_change:
            for name in nodes_to_run:
                self.on_state_change(name, NodeState.PENDING)

        cpu_sem = threading.Semaphore(self.cpu_workers)
        gpu_sem = threading.Semaphore(self.gpu_workers)
        threads = []

        while True:
            if self._stop.is_set() and not self._running:
                break
            if not self._pending and not self._running:
                break

            with self._lock:
                ready = [n for n in list(self._pending) if self._can_run(n)]

            for name in ready:
                with self._lock:
                    if name not in self._pending:
                        continue
                    self._pending.discard(name)
                    self._running.add(name)
                    self._states[name] = NodeState.RUNNING
                if self.on_state_change:
                    self.on_state_change(name, NodeState.RUNNING)

                node = self.graph.get_node(name)
                is_gpu = self._is_gpu_node(node)
                sem = gpu_sem if is_gpu else cpu_sem

                t = threading.Thread(
                    target=self._run_node,
                    args=(name, pipeliner, use_cache, sem),
                    daemon=True,
                )
                threads.append(t)
                t.start()

            time.sleep(0.05)

        for t in threads:
            t.join()

        return len(self._failed) == 0

    def _can_run(self, name):
        if name in self._running or name in self._executed or name in self._failed:
            return False
        if self._stop.is_set():
            return False
        for dep in self.graph.get_dependencies(name):
            if dep not in self._executed:
                return False
            if dep in self._failed:
                return False
        return True

    def _run_node(self, name, pipeliner, use_cache, sem):
        with sem:
            if self._stop.is_set():
                with self._lock:
                    self._running.discard(name)
                    self._states[name] = NodeState.SKIPPED
                if self.on_state_change:
                    self.on_state_change(name, NodeState.SKIPPED)
                return

            try:
                def _line_cb(line):
                    if self.line_callback:
                        self.line_callback(name, line)

                # trigger_jump_forward=False: a full-graph run already
                # includes every jump successor via the normal
                # dependency-ordered schedule (get_dependencies), which
                # respects cpu/gpu worker limits. Cascading here too would
                # run a node synchronously inside this thread, bypassing
                # those limits.
                success = pipeliner.run_node(
                    name,
                    graph=self.graph,
                    use_cache=use_cache,
                    debug=self.debug,
                    line_callback=_line_cb,
                    trigger_jump_forward=False,
                )

                with self._lock:
                    self._running.discard(name)
                    if success:
                        self._executed.add(name)
                        self._states[name] = NodeState.COMPLETED
                        new_state = NodeState.COMPLETED
                        err = None
                    else:
                        self._failed.add(name)
                        self._states[name] = NodeState.FAILED
                        new_state = NodeState.FAILED
                        err = f'Node {name} returned non-zero exit code.'
                        self._error_info[name] = err
                        self._stop.set()
                        self._skip_dependents(name)

                if self.on_state_change:
                    self.on_state_change(name, new_state, error=err)

            except Exception as e:
                err = str(e)
                with self._lock:
                    self._running.discard(name)
                    self._failed.add(name)
                    self._states[name] = NodeState.FAILED
                    self._error_info[name] = err
                    self._stop.set()
                    self._skip_dependents(name)
                if self.on_state_change:
                    self.on_state_change(name, NodeState.FAILED, error=err)

    def _skip_dependents(self, failed_name):
        """Must be called with self._lock held."""
        for name in list(self._pending):
            deps = self.graph.get_dependencies(name)
            if failed_name in deps or any(d in self._failed for d in deps):
                self._pending.discard(name)
                self._states[name] = NodeState.SKIPPED
                if self.on_state_change:
                    self.on_state_change(name, NodeState.SKIPPED)
                self._skip_dependents(name)

    def _is_gpu_node(self, node):
        if node is None:
            return False
        return getattr(node, 'category', '').lower() in ('gpu', 'training', 'inference')

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def get_states(self):
        with self._lock:
            return dict(self._states)

    def get_errors(self):
        with self._lock:
            return dict(self._error_info)

    def stop(self):
        self._stop.set()
