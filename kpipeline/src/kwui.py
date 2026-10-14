"""kwui mode: drive a pipeline run from a kwebui web UI.

Two halves live here, on opposite sides of a process boundary:

* **Node side** (`UIProxy`) — runs *inside* a node subprocess and is what a
  node author touches as `self.ui`. It owns no widgets; every
  `self.ui.text(...)` call is serialized to a `<KWUI> {json} </KWUI>` line on
  the node's real stdout, and every callback the author passes
  (`on_click=self.go`) stays a live Python object here, keyed by widget id.
* **Parent side** (`KwuiBridge`) — runs in the `kpl` process alongside the
  kwebui app, translates those lines into real widget calls on the workflow's
  `app_container`, and pushes browser events back down the node's stdin so the
  author's callback fires in the process that defined it.

Only one node is attached at a time (kwui runs are sequential), so the bridge
keeps a single stdin handle and a single id→widget map, both reset per node.

Method-forwarding is generic (any widget/method name kwebui adds "just
works," no per-widget code needed here) — the exceptions, all handled below,
are: KApp's OWN methods (`set_theme`/`on_shutdown`/`exit` — see
`_APP_METHODS`, routed to the app object, not treated as a widget type);
child widgets created on a container-like widget (`box.text(...)`,
`columns[0].button(...)` — the node picks the child's id itself and gets
back a real, addressable `UIWidgetRef`, since there's no return channel to
learn kwebui's own internally-assigned id); columns indexing (kwebui gives
each column a deterministic id, so no round trip is needed there either);
and anything that isn't already JSON-safe (raw `bytes`, a pandas-like
DataFrame) or is a live Python callable, both encoded/decoded transparently.
NOT supported: reading anything back synchronously (`imagestream`'s pull-model
`frame_provider` needs kwebui to receive a return value from a node-side
callable, which this one-way relay structurally cannot do — use `push_frame`
instead, from a background thread in the node), and `KApp.session` (a
property, same reason).
"""

import base64
import difflib
import itertools
import json
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser

KWUI_OPEN = '<KWUI>'
KWUI_CLOSE = '</KWUI>'

# Marks a kwargs entry that was a callable on the node side. The parent
# substitutes a lambda that relays the browser event back over stdin.
CB_MARKER = '__kwui_cb__'

# Marks an arg/kwargs entry that was raw bytes on the sending side — bytes
# aren't JSON-serializable, so both directions base64-encode them under this
# marker rather than silently dropping the call (json.dumps would otherwise
# raise inside _send(), which only logs and swallows the message). This is
# what makes e.g. imagestream's push_frame(jpeg_bytes) work across the
# node/parent process boundary.
BYTES_MARKER = '__kwui_bytes__'


def _encode_value(value):
    if isinstance(value, (bytes, bytearray)):
        return {BYTES_MARKER: base64.b64encode(bytes(value)).decode('ascii')}
    if hasattr(value, 'columns') and hasattr(value, 'values'):
        # DataFrame-like (pandas, ...). kwebui's own `table` widget already
        # accepts this shape by duck-typing (kwebui/widgets/table.py's
        # `_normalize_table_data`) — but only once the object has safely
        # crossed this JSON-only bridge, and a raw DataFrame can't (json.dumps
        # would raise in _send() below and the whole call gets dropped).
        # Pre-convert to the equivalent list-of-dicts shape kwebui also
        # natively accepts (ndarray.tolist() already yields plain Python
        # scalars, so this is JSON-safe), so `self.ui.table(df)` keeps working
        # exactly like calling kwebui directly in the same process would.
        try:
            columns = list(value.columns)
            rows = value.values.tolist()
            return [dict(zip(columns, row)) for row in rows]
        except Exception:
            pass  # fall through; _send()'s json.dumps will raise+log instead
    return value


def _decode_bytes_value(value):
    if isinstance(value, dict) and BYTES_MARKER in value:
        return base64.b64decode(value[BYTES_MARKER])
    return value


STATUS_OKAY = 'OKAY'
STATUS_ERROR = 'ERROR'
STATUS_REPEAT = 'REPEAT'
#: Like ERROR as far as the step icon goes, but the workflow carries on with the next
#: step: the node counts as done (exit code 0), only the tracker shows "error".
STATUS_ERROR_SKIP = 'ERROR_SKIP'
#: Only ever produced by end('JUMP', <node>) / jump_to(<node>) — `end('JUMP')`
#: WITHOUT a target is still just a data message (status OKAY), as before.
STATUS_JUMP = 'JUMP'


def _status_of(msg):
    """The runner-facing status for an `end(msg)` argument: OKAY / ERROR /
    ERROR_SKIP / REPEAT when msg says so (case-insensitive), OKAY for any other
    message — arbitrary messages are data for `interact()`, not an outcome."""
    status = str(msg).strip().upper()
    known = (STATUS_OKAY, STATUS_ERROR, STATUS_ERROR_SKIP, STATUS_REPEAT)
    return status if status in known else STATUS_OKAY

# The node subprocess may run in its own venv without kwebui, so it cannot ask
# the registry which widgets exist. The parent puts the list in this env var
# instead — see KNode.execute(). Its purpose is to make a MISSPELLED call fail
# loudly: `self.ui` resolves unknown names as widget factories, so without this
# `self.ui.intract()` would just create a widget type nobody has and run on.
WIDGETS_ENV = 'KPL_KWUI_WIDGETS'

#: UIProxy's own methods — valid on `self.ui` but not widget names.
_UI_METHODS = ('interact', 'end', 'clear', 'jump_to')

#: KApp's own methods (kwebui/app.py) — also valid on `self.ui`, but routed to
#: the app object itself rather than treated as "create a widget of this
#: type" (see UIProxy.__getattr__/KwuiBridge._app_call). Unlike widgets,
#: KApp isn't a plugin registry — there's no live list to check against, so
#: this is a small fixed set kept in sync with kwebui/app.py by hand. Not
#: included: `run`/`__init__` (lifecycle, not callable from a running node),
#: `session` (a property, and reading state back would need a synchronous
#: return channel this bridge doesn't have — see the module docstring).
_APP_METHODS = ('set_theme', 'on_shutdown', 'exit')


def available_widget_names():
    """Every widget name kwebui currently registers, or None if kwebui isn't
    importable here (then validation is skipped rather than guessed at)."""
    try:
        from kwebui.registry import WidgetRegistry
    except Exception:
        return None
    try:
        return sorted(WidgetRegistry().discover().names())
    except Exception:
        return None


def _allowed_widget_names():
    raw = os.environ.get(WIDGETS_ENV, '')
    return {n for n in raw.split(',') if n} or None


def _unknown_call_error(name, allowed):
    candidates = sorted(allowed) + list(_UI_METHODS) + list(_APP_METHODS)
    suggestions = difflib.get_close_matches(name, candidates, n=1)
    hint = f" Did you mean '{suggestions[0]}'?" if suggestions else ''
    return AttributeError(f"self.ui has no widget or method '{name}'.{hint}")


# ---------------------------------------------------------------------------
# Node side — `self.ui`
# ---------------------------------------------------------------------------

class UIWidgetRef:
    """Node-side handle for a widget that actually lives in the parent.

    Most attributes resolve to a fire-and-forget METHOD call forwarded to the
    real widget (`ref.update(text='x')`, `ref.set_task_status(...)`,
    `ref.highlight()`) — these return `self` so chaining works; there is no
    return-value round-trip, so a method that reads state back (e.g.
    `latest_frame()`) is not usable through the bridge.

    An attribute whose name matches a WIDGET TYPE instead (e.g.
    `container_ref.text(...)`, `sidebar_ref.button(...)`, `columns[0].slider
    (...)`) is a CHILD-creating call — this mirrors how kwebui's own
    Container/Empty/Sidebar/Topbar/Column widgets use `__getattr__` as a
    dynamic child factory. Since there's no return channel to learn kwebui's
    own internally-assigned child id, the node picks its OWN id up front and
    tells the parent to file the created child under it (KwuiBridge._create_
    child) — so, unlike a plain method call, this DOES return an addressable
    new `UIWidgetRef` for that child, usable later exactly like any top-level
    widget (`label = box.text("hi"); ...; label.set_text("bye")`).

    Discrimination is by name only, checked against the same live widget
    registry `self._proxy._allowed` uses — so it degrades the same way
    everything else keyed off that list does: if the registry isn't
    available (kwebui not importable in this node's env), a call
    conservatively falls back to the plain "method call on self" behavior.
    """

    def __init__(self, proxy, widget_id):
        self._proxy = proxy
        self._id = widget_id

    def _is_widget_type(self, name):
        allowed = self._proxy._allowed
        return allowed is not None and name in allowed

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        if self._is_widget_type(name):
            def child_factory(*args, **kwargs):
                child_id = self._proxy._next_id()
                self._proxy._send({
                    'op': 'create_child', 'parent': self._id, 'id': child_id,
                    'type': name,
                    'args': self._proxy._encode_args(child_id, args),
                    'kwargs': self._proxy._encode_kwargs(child_id, kwargs),
                })
                return UIWidgetRef(self._proxy, child_id)

            return child_factory

        def sender(*args, **kwargs):
            self._proxy._send({
                'op': 'call', 'id': self._id, 'method': name,
                'args': self._proxy._encode_args(self._id, args),
                'kwargs': self._proxy._encode_kwargs(self._id, kwargs),
            })
            return self

        return sender

    # Columns only: kwebui's ColumnsWidget assigns each child a DETERMINISTIC
    # id, f"{widget_id}-c{i}" (kwebui/widgets/columns.py, ColumnsPlugin.
    # create) — the one place in kwebui a child id is predictable without a
    # return channel, so indexing/iterating can be plain local id math, no
    # protocol round trip needed. `_count` is stashed by UIProxy's factory
    # when it sees `type == 'columns'`; anything else raises TypeError, same
    # as indexing a real Python object that doesn't support it.
    def __getitem__(self, index):
        count = self.__dict__.get('_count')
        if count is None:
            raise TypeError(f"'{self._id}' does not support indexing (not a columns widget)")
        if not (0 <= index < count):
            raise IndexError(index)
        return UIWidgetRef(self._proxy, f'{self._id}-c{index}')

    def __len__(self):
        count = self.__dict__.get('_count')
        if count is None:
            raise TypeError(f"'{self._id}' has no length (not a columns widget)")
        return count

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    # `with self.ui.spinner("Working...", show_time=True):` — forwarded to
    # the real widget's OWN __enter__/__exit__ (spinner's ticks `elapsed`
    # via an asyncio task; sidebar/topbar's are harmless no-ops; anything
    # else without a real __enter__/__exit__ also no-ops, silently, via
    # KwuiBridge._call's special-case for these two names — see there).
    def __enter__(self):
        self._proxy._send({'op': 'call', 'id': self._id, 'method': '__enter__', 'args': [], 'kwargs': {}})
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self._proxy._send({'op': 'call', 'id': self._id, 'method': '__exit__', 'args': [], 'kwargs': {}})
        return False


class UIProxy:
    """`self.ui` inside a kwui-enabled node running under a live bridge."""

    def __init__(self, node):
        self._node = node
        self._counter = itertools.count(1)
        self._handlers = {}
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._message = STATUS_OKAY  # what interact() returns; set by end()
        self._allowed = _allowed_widget_names()
        threading.Thread(target=self._read_events, daemon=True).start()

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        # KApp's own methods (set_theme, on_shutdown, exit) — routed to the
        # app object itself, not treated as "create a widget of this type".
        # See _APP_METHODS and KwuiBridge._app_call.
        if name in _APP_METHODS:
            def app_caller(*args, **kwargs):
                self._send({
                    'op': 'app_call', 'method': name,
                    'args': self._encode_args('__app__', args),
                    'kwargs': self._encode_kwargs('__app__', kwargs),
                })
                return None  # fire-and-forget; not chainable like a widget ref

            return app_caller

        if self._allowed is not None and name not in self._allowed:
            raise _unknown_call_error(name, self._allowed)

        def factory(*args, **kwargs):
            widget_id = self._next_id()
            self._send({
                'op': 'create', 'id': widget_id, 'type': name,
                'args': self._encode_args(widget_id, args),
                'kwargs': self._encode_kwargs(widget_id, kwargs),
            })
            ref = UIWidgetRef(self, widget_id)
            if name == 'columns' and (args or 'n' in kwargs):
                # Stash the column count locally so UIWidgetRef.__getitem__/
                # __len__/__iter__ work without a return channel — see there.
                n = args[0] if args else kwargs.get('n')
                try:
                    ref._count = n if isinstance(n, int) else len(n)
                except TypeError:
                    pass
            return ref

        return factory

    def clear(self):
        """Wipe everything this node put in the workflow container."""
        self._send({'op': 'clear'})

    def interact(self, timeout=None):
        """Block until something calls `end()` — normally a button callback
        defined in this same node.py. Returns exactly what `end()` was given
        (any Python object, unchanged), or None if `timeout` (seconds)
        elapsed first."""
        self._done.clear()
        self._send({'op': 'interact'})
        if not self._done.wait(timeout):
            return None
        return self._message

    def end(self, msg=STATUS_OKAY, arg=None, *, target=None, detail=None):
        """Report this node's UI outcome and release a waiting `interact()`,
        which returns `msg` unchanged.

        "OKAY" (the default) -> the node succeeded; "ERROR" -> fail the node
        even if it exits 0, which STOPS the workflow at this step; "ERROR_SKIP"
        -> the step is shown as an error but the workflow carries on with the next
        step (the node counts as done, so its saved outputs stay usable);
        "REPEAT" -> the runner re-executes this node once it exits; "JUMP" +
        a node name -> the node succeeds and the workflow continues at that
        step/node (earlier or later — see jump_to). All matched case-insensitively.
        Any OTHER message is just data for `interact()`'s caller — the node counts
        as succeeded. `end("JUMP")` without a target is such a plain data message.

        The optional second argument depends on the outcome: for "JUMP" it is the
        target node (`target=` also works); for "ERROR" / "ERROR_SKIP" it is a short
        text shown under the step in the workflow's step list (`detail=` also works),
        e.g. `self.ui.end("ERROR_SKIP", "camera not found")`.
        """
        status = _status_of(msg)
        payload = {'op': 'end', 'status': status}
        if str(msg).strip().upper() == STATUS_JUMP:
            goal = target if target else arg
            if goal:
                status = STATUS_JUMP
                payload = {'op': 'end', 'status': STATUS_JUMP, 'target': str(goal)}
        elif status in (STATUS_ERROR, STATUS_ERROR_SKIP):
            text = detail if detail is not None else arg
            if text is not None and str(text).strip():
                payload['detail'] = str(text).strip()
        self._message = msg
        self._send(payload)
        self._done.set()
        return status

    def jump_to(self, node_name):
        """Finish this node and continue the workflow at `node_name` — a node
        of the workflow's sequence, EARLIER (redo from there) or LATER (skip
        ahead). Same as `end("JUMP", node_name)`. A name that is not in the
        sequence fails this node with a message listing the valid names."""
        return self.end(STATUS_JUMP, node_name)

    # -- internals ----------------------------------------------------------

    def _next_id(self):
        return f'n{next(self._counter)}'

    def _encode_call_value(self, widget_id, key, value):
        """Shared by both args and kwargs: a callable is registered as a
        handler keyed by (widget_id, key) — key is the kwarg name for a
        kwarg (e.g. 'on_click'), or a synthetic '_argN' for a positional
        callable (e.g. KApp.on_shutdown(callback), the one kwebui callback
        that isn't keyword-only). Either way the SAME key travels in the
        CB_MARKER and comes back unchanged in the browser-event's 'cb' field
        (KwuiBridge._decode/_send_event), so lookup in _read_events matches
        regardless of which shape produced it."""
        if callable(value):
            with self._lock:
                self._handlers[(widget_id, key)] = value
            return {CB_MARKER: key}
        return _encode_value(value)

    def _encode_args(self, widget_id, args):
        return [self._encode_call_value(widget_id, f'_arg{i}', v) for i, v in enumerate(args)]

    def _encode_kwargs(self, widget_id, kwargs):
        return {key: self._encode_call_value(widget_id, key, value) for key, value in kwargs.items()}

    def _send(self, message):
        try:
            payload = json.dumps(message)
        except TypeError as exc:
            self._node.log(f'kwui: UI call is not JSON-serializable ({exc})', 'error')
            return
        # Real stdout, never sys.stdout: _PrintCapture has replaced the latter
        # by now and would swallow the line into the log file instead of
        # letting the parent's <KWUI> parser see it (same reason
        # send_status_message() does this for <KPM>).
        stream = getattr(self._node, '_real_stdout', None) or sys.stdout
        stream.write(f'{KWUI_OPEN} {payload} {KWUI_CLOSE}\n')
        stream.flush()

    def _read_events(self):
        stream = sys.__stdin__ or sys.stdin
        while True:
            try:
                line = stream.readline()
            except Exception:
                return
            if not line:
                return
            try:
                message = json.loads(line.strip())
            except Exception:
                continue
            if message.get('op') != 'event':
                continue
            with self._lock:
                callback = self._handlers.get((message.get('id'), message.get('cb')))
            if callback is None:
                continue
            # One thread per event: a callback that blocks (or one that calls
            # end() and then keeps working) must not stall later clicks.
            # Decode bytes back (file_uploader's on_upload(filename, data:
            # bytes) is the one built-in callback that receives them —
            # KwuiBridge._send_event/_jsonable base64-encodes under
            # BYTES_MARKER on the way out for exactly this reason).
            args = [_decode_bytes_value(v) for v in (message.get('args') or [])]
            threading.Thread(target=self._invoke, args=(callback, args), daemon=True).start()

    def _invoke(self, callback, args):
        try:
            callback(*args)
        except Exception:
            self._node.log(f'kwui callback failed:\n{traceback.format_exc()}', 'error')


class NoopUI:
    """`self.ui` for a kwui-enabled node running *without* a bridge (a plain
    `kpl run`). Every widget call is discarded and `interact()` returns
    immediately, so the same node.py works in both modes unchanged."""

    def __init__(self):
        self._allowed = _allowed_widget_names()

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        # Still validated: a misspelled widget must fail in a plain `kpl run`
        # too, not only once someone tries the UI. _APP_METHODS (set_theme,
        # on_shutdown, exit) are valid here too, same as on the live UIProxy.
        if name not in _APP_METHODS and self._allowed is not None and name not in self._allowed:
            raise _unknown_call_error(name, self._allowed)

        def noop(*args, **kwargs):
            return self if name not in _APP_METHODS else None

        return noop

    # Mirrors UIWidgetRef.__enter__/__exit__ — without this, `with
    # self.ui.spinner(...):` would work under `kpl run --kwui` (a live
    # UIWidgetRef) but crash under a plain `kpl run` (self.ui.spinner(...)
    # returns this NoopUI itself via __getattr__ above), breaking the one
    # guarantee this class exists for: identical node.py code in both modes.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        return False

    # Mirrors UIWidgetRef.__getitem__/__len__/__iter__ (columns) — same
    # "never crash" contract as everything else here. Real column COUNT
    # isn't tracked (this singleton-like object discards args on every
    # call), so len()/iteration report empty rather than a wrong count;
    # indexing still returns something usable (itself) instead of raising,
    # so `self.ui.columns(3)[0].text(...)` doesn't crash a plain `kpl run`.
    def __getitem__(self, index):
        return self

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def clear(self):
        return self

    def interact(self, timeout=None):
        return STATUS_OKAY

    def end(self, msg=STATUS_OKAY, arg=None, *, target=None, detail=None):
        if str(msg).strip().upper() == STATUS_JUMP and (target or arg):
            return STATUS_JUMP
        return _status_of(msg)

    def jump_to(self, node_name):
        return STATUS_JUMP


# ---------------------------------------------------------------------------
# Parent side — the bridge
# ---------------------------------------------------------------------------

_active_bridge = None


def get_active_bridge():
    return _active_bridge


# Buttons in workflow.py call these (see kwui_workflow_template.py). They accept
# and ignore any arguments so they can be passed directly as `on_click=...`, and
# do nothing outside a live kwui run. (Reloading the browser page is client-side
# and needs nothing from here — see the template's "Refresh page" button.)

def reset_all(*_args):
    """Start the whole workflow over from its first step."""
    bridge = get_active_bridge()
    if bridge is not None:
        bridge.reset_all()


def restart_step(*_args):
    """Run the current step again: stops it if it is running (everything it started),
    and rebuilds its page. After a failure it is the step that failed."""
    bridge = get_active_bridge()
    if bridge is not None:
        bridge.restart_current()


def continue_workflow(*_args):
    """After the workflow stopped on an error, go on with the next step (the failed
    step stays shown as an error). Does nothing, and says so, if nothing is stopped."""
    bridge = get_active_bridge()
    if bridge is not None:
        bridge.continue_after_error()


def refresh_step(*_args):
    """Kept only so a workflow.py copied from an EARLIER template (button "Refresh
    step") still imports and runs. A refresh no longer touches the workflow: it is
    a plain browser reload (F5), done client-side — see the template's "Refresh
    page" button — so there is nothing for the server to do."""
    print('[kwui] "Refresh step" no longer restarts a step. Reload the page with F5 '
          '(or use the "Refresh page" button of the current workflow.py template); '
          'use "Reset all" to start over.', flush=True)


def jump_to_step(node_name):
    """Continue the workflow at `node_name` (a button of your own can use this)."""
    bridge = get_active_bridge()
    if bridge is not None:
        bridge.request_jump(node_name, 'user')


def set_active_bridge(bridge):
    global _active_bridge
    _active_bridge = bridge


def notify_state(node_name, state):
    """Called from Pipeliner._on_state_change — the one choke point both the
    sequential and parallel run paths already go through."""
    bridge = _active_bridge
    if bridge is None:
        return
    try:
        bridge.on_node_state(node_name, state)
    except Exception:
        pass  # the UI must never break a run


def _jsonable(values):
    safe = []
    for value in values:
        if isinstance(value, (bytes, bytearray)):
            # file_uploader's on_upload(filename, data: bytes) is the one
            # built-in callback kwebui invokes with raw bytes — without this,
            # the `except TypeError` below would replace it with str(value)
            # (a repr string), silently corrupting the uploaded file's
            # content rather than failing loudly. UIProxy._read_events
            # decodes this back before invoking the node's callback.
            safe.append({BYTES_MARKER: base64.b64encode(bytes(value)).decode('ascii')})
            continue
        try:
            json.dumps(value)
        except TypeError:
            value = str(value)
        safe.append(value)
    return safe


#: Environment variable that marks every process started by one node run of a kwui
#: workflow (value: a unique token) — see KwuiBridge.stop_run / KNode.execute.
RUN_TOKEN_ENV = 'KPL_KWUI_RUN_TOKEN'

#: After a node asks for a JUMP (end("JUMP", ...) / jump_to) it may keep running
#: for this many seconds (saving outputs, closing devices); then its processes are
#: stopped so the workflow can actually move on.
JUMP_STOP_GRACE = 5.0

#: Pseudo jump target meaning "go on with the step after the one that failed".
CONTINUE = '__continue__'

#: Page key shared by every node that is not a tracker step (data dependencies
#: pulled in implicitly) — there is no step to click to reach their page.
BACKGROUND_PAGE = '__background__'

#: A step can be opened from the tracker once it has started.
_VIEWABLE = ('in-progress', 'completed', 'error')


def _disable_interactive(widget):
    """Make every widget that has a callback inert (visibly dimmed, and the
    server drops its events). Cosmetic belt-and-braces on top of the bridge's own
    stale-event guard — and a no-op on a kwebui without Widget.disable()."""
    try:
        if hasattr(widget, 'disable') and any(callable(v) for v in widget.props.values()):
            widget.disable()
        for child in widget.children:
            _disable_interactive(child)
    except Exception:
        pass


def _stop_activity(widget):
    """Stop the SERVER-side activity of widgets a node left behind and can no longer
    end itself. Today that is the timer of `spinner(show_time=True)`: kwebui ticks its
    elapsed time on the server until the node's `with` block exits — which never
    happens for a node that was stopped — and every tick broadcasts an update, so
    after the page was removed the browser would re-create the widget as a stray,
    still-counting spinner. Exiting it cancels the timer (and shows it as finished)."""
    try:
        if getattr(widget, '_tick_task', None) is not None:
            widget.__exit__(None, None, None)
        for child in list(widget.children):
            _stop_activity(child)
    except Exception:
        pass


class KwuiBridge:
    """Parent side of the node<->UI bridge: replays a node's widget calls onto
    workflow.py's `app_container`, relays browser events back down, and — new —
    owns the workflow's control plane (jump / reset) and, optionally,
    the per-step page history.

    History (`app.history`, default True; see _history_enabled): each tracker
    step gets its own page inside `app_container`; only the running step's page
    is ever written to, the others are merely hidden, so clicking a finished
    step in the tracker shows what it last displayed. With `history = False`
    — or a workflow.py that brings its own container/tracker objects — the
    original behavior applies unchanged: one container, wiped at every node
    start."""

    def __init__(self, tracker, container, app=None):
        self.tracker = tracker
        self.container = container
        self.app = app  # the KApp itself — only needed for _APP_METHODS calls
        self._widgets = {}
        self._stdin = None
        self._stdin_lock = threading.Lock()
        self._statuses = {}
        self._fresh = True
        # Stale-event guard: every attach() (= every node run) gets a token;
        # a callback remembers the token of the run that created its widget and
        # is dropped unless that run is the one attached right now.
        self._run_counter = 0
        self._live_token = None
        self._reading_token = None
        # History pages (see class docstring).
        self._pages = {}
        self._target = None
        self._status = {}
        self._step_ids = set()
        self._history = self._history_enabled(app, tracker, container)
        if self._history:
            self._init_history()
        # Control plane: pending jump = (target step, origin 'node' | 'user').
        self._cond = threading.Condition()
        self._pending = None
        self._proc = None
        self._proc_token = None
        self._proc_node = None
        self._error_skipped = set()   # nodes that reported ERROR_SKIP in their current run
        self._details = {}            # node -> text to show under its step (from end(..., "text"))
        self._stopped_on = None       # step the workflow is stopped at because of an error (else None)
        self._skipped_log = []        # steps that ended with a skipped error (for the end-of-run note)
        self._token_nodes = {}   # run token -> node name, for every node run of this session
        self._stopper = None
        self.sequence = []
        self.current_step = None

    # -- history ------------------------------------------------------------

    @staticmethod
    def _history_enabled(app, tracker, container):
        """On unless workflow.py says `history = False` — and only for the
        plain kwebui widgets the template creates. A workflow.py that hands in
        its own container/tracker stand-ins (own page management) is left alone."""
        if not getattr(app, 'history', True):
            return False
        try:
            from kwebui.widget import Widget
        except Exception:
            return False
        return (isinstance(container, Widget) and isinstance(tracker, Widget)
                and hasattr(container, 'clear'))

    def _init_history(self):
        try:
            self._step_ids = {task['id'] for task in self.tracker.props.get('tasks', [])}
        except Exception:
            self._step_ids = set()
        # Attach the tracker click handler unless workflow.py set its own. The
        # tracker reads `on_select` from its props when a click arrives, and
        # callables are never sent to the browser, so setting it late is the
        # same as passing it to workflow_tracker(...).
        try:
            if self.tracker.props.get('on_select') is None:
                self.tracker.update(on_select=self._on_step_select)
        except Exception:
            self._history = False

    @property
    def _out(self):
        """Where the running node's widgets go: its page, else the container."""
        return self._target if self._target is not None else self.container

    def _begin_page(self, node_name):
        if not self._history:
            return
        key = node_name if node_name in self._step_ids else BACKGROUND_PAGE
        page = self._pages.get(key)
        if page is None:
            page = self._pages[key] = self.container.container(border=False)
        self._target = page
        self._show_page(key)

    def _show_page(self, key):
        for page_key, page in list(self._pages.items()):
            try:
                page.show() if page_key == key else page.hide()
            except Exception:
                pass

    def _on_step_select(self, task_id):
        if self._status.get(task_id) in _VIEWABLE and task_id in self._pages:
            self._show_page(task_id)

    def _freeze(self, node_name):
        if not self._history:
            return
        page = self._pages.get(node_name if node_name in self._step_ids else BACKGROUND_PAGE)
        if page is not None:
            for child in list(page.children):
                _stop_activity(child)
                _disable_interactive(child)

    # -- node lifecycle -----------------------------------------------------

    def on_node_state(self, node_name, state):
        if state == 'RUNNING':
            self._widgets.clear()
            self._fresh = True
            self._error_skipped.discard(node_name)
            self._details.pop(node_name, None)
            self._set_step(node_name, 'in-progress')
            self._begin_page(node_name)
            _stop_activity(self._out)
            self._out.clear()
            # Name the running node — a data dependency pulled in implicitly
            # (not a tracker step) can take a while (env install, heavy
            # compute); a content-free "Working ..." looks identical to a
            # hung page for as long as that takes.
            self._out.spinner(f'Working ... ({node_name})').update(active=True)
        elif state == 'COMPLETED':
            if node_name in self._error_skipped:
                # ERROR_SKIP: done as far as the pipeline is concerned, but the step
                # shows the error and the workflow simply goes on.
                self._error_skipped.discard(node_name)
                if self._set_step(node_name, 'error', self._details.pop(node_name, '')):
                    if node_name not in self._skipped_log:
                        self._skipped_log.append(node_name)
                    print(f"[kwui] Step '{node_name}' ended with ERROR_SKIP: shown as an "
                          f"error, the workflow continues.", flush=True)
            else:
                self._set_step(node_name, 'completed')
            _stop_activity(self._out)
            self._freeze(node_name)
        elif state == 'FAILED':
            # A node stopped on purpose (Reset) is not an error.
            self._error_skipped.discard(node_name)   # it crashed after all: a real failure
            if not self._user_abort_pending():
                self._set_step(node_name, 'error', self._details.pop(node_name, ''))
            else:
                self._details.pop(node_name, None)
            _stop_activity(self._out)
            self._freeze(node_name)

    def _set_step(self, node_name, status, detail=''):
        """Set a step's status; `detail` is the text under it ('' clears it)."""
        try:
            self.tracker.set_task_status(node_name, status, detail)
            self._status[node_name] = status
            return True
        except ValueError:
            return False  # not a tracker step (a data dependency pulled in implicitly)

    def attach(self, node_name, stdin, process=None):
        with self._stdin_lock:
            self._stdin = stdin
            self._run_counter += 1
            self._live_token = self._run_counter
        self._statuses.pop(node_name, None)

    def track_process(self, process, token, node_name=None):
        """A node's process started (ANY node of the run, UI or not): remember it so
        a Reset / jump can stop it."""
        self._proc = process
        self._proc_token = token
        self._proc_node = node_name
        self._token_nodes[token] = node_name

    def untrack_process(self):
        self._proc = None
        self._proc_token = None
        self._proc_node = None

    def detach(self, node_name):
        with self._stdin_lock:
            self._stdin = None
            self._live_token = None

    def consume_status(self, node_name):
        return self._statuses.pop(node_name, None)

    # -- control plane: jump / reset ------------------------------------------

    def set_sequence(self, sequence):
        self.sequence = list(sequence)

    def note_error_skip(self, node_name, why):
        """A node of this run crashed and its type says `on_error: skip`: treat it exactly
        like ERROR_SKIP (see KNode.execute). The node's own text, if it gave one, wins."""
        self._error_skipped.add(node_name)
        self._details.setdefault(node_name, why)

    def restart_current(self):
        """Run the current step again (stopping it first if it is running)."""
        step = self.current_step or (self.sequence[0] if self.sequence else None)
        if step:
            self.request_jump(step, 'user')

    def set_stopped_on(self, node_name):
        with self._cond:
            self._stopped_on = node_name

    def continue_after_error(self):
        """Carry on with the next step after the workflow stopped on an error."""
        with self._cond:
            if self._stopped_on is None:
                accepted = False
            else:
                accepted = True
                self._pending = (CONTINUE, 'user', False)
                self._cond.notify_all()
        if not accepted:
            print('[kwui] "Continue" only applies when the workflow has stopped because '
                  'of an error — nothing to continue.', flush=True)
        return accepted

    def mark_continued(self, node_name):
        """The workflow went on past step `node_name`, which had failed."""
        if node_name not in self._skipped_log:
            self._skipped_log.append(node_name)
        print(f"[kwui] Continuing after the error in step '{node_name}'.", flush=True)

    def skipped_error_steps(self):
        """Steps that ended with ERROR_SKIP in this workflow run (still counted until reset)."""
        return list(self._skipped_log)

    def begin_step(self, name):
        with self._cond:
            self.current_step = name

    def request_jump(self, target, origin):
        """Ask the run loop to continue at `target` (a node of the sequence).
        origin 'node': the node asked via end('JUMP', ...) and ends by itself.
        origin 'user': Reset — the running node is stopped (everything it started). Returns False, with a message, for an unknown target."""
        if target not in self.sequence:
            print(f"[kwui] cannot jump to {target!r}: not in the workflow sequence "
                  f"({', '.join(self.sequence) or 'empty'}).", flush=True)
            return False
        with self._cond:
            self._pending = (target, origin, False)   # (target, origin, forced stop)
            proc, token, node = self._proc, self._proc_token, self._proc_node
            self._cond.notify_all()
        if proc is not None:
            # In a thread: waiting for the processes to die must not block the
            # caller (the browser-event handler / the node's output reader).
            if origin == 'user':
                target_fn, args = self.stop_run, (proc, token)
            else:
                target_fn, args = self._jump_watchdog, (proc, token, node)
            self._stopper = threading.Thread(target=target_fn, args=args, daemon=True)
            self._stopper.start()
        return True

    def _jump_watchdog(self, proc, token, node_name):
        """The node asked to jump: let it exit by itself (it may still be saving
        something) for JUMP_STOP_GRACE seconds, then stop it and everything it
        started — otherwise a node that keeps its process alive would hold the
        workflow on this step forever."""
        deadline = time.time() + JUMP_STOP_GRACE
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        if proc.poll() is None:
            with self._cond:
                if self._pending is not None:
                    self._pending = (self._pending[0], self._pending[1], True)
            print(f"[kwui] Node {node_name!r} asked for a jump but its process kept running "
                  f"for {JUMP_STOP_GRACE:g}s - stopping it.", flush=True)
            self.stop_run(proc, token)

    def reset_all(self):
        """Start over from the first step."""
        if self.sequence:
            self.request_jump(self.sequence[0], 'user')

    def _user_abort_pending(self):
        """A stop is under way on purpose (the user's Reset, or a jump's grace period
        ran out) — the node ending then is not an error."""
        with self._cond:
            return self._pending is not None and (self._pending[1] == 'user' or self._pending[2])

    def take_jump(self, ok=True):
        """The pending jump target, if any (consuming it). A node that asked
        for a jump but then FAILED stays a failure; a user's Reset
        always counts — the node was stopped on purpose."""
        with self._cond:
            pending, self._pending = self._pending, None
        if pending is None:
            return None
        target, origin, forced = pending
        return None if (origin == 'node' and not ok and not forced) else target

    def wait_for_jump(self):
        """Block until a jump is requested (the workflow has finished or
        stopped on a failure); returns its target."""
        with self._cond:
            while self._pending is None:
                self._cond.wait()
            pending, self._pending = self._pending, None
        return pending[0]

    def reset_steps(self, names):
        """Steps `names` start over: tracker back to pending, pages dropped."""
        background = self._pages.get(BACKGROUND_PAGE)
        if background is not None:
            _stop_activity(background)
        for name in names:
            self._set_step(name, 'pending')
            self._status.pop(name, None)
            if name in self._skipped_log:
                self._skipped_log.remove(name)
            page = self._pages.pop(name, None)
            if page is not None:
                _stop_activity(page)
                if self._target is page:
                    self._target = None
                try:
                    page.remove()
                except Exception:
                    pass

    @staticmethod
    def _run_processes(process, token):
        """Every process belonging to one node run: the shell kpl started and its
        whole tree, PLUS anything else carrying the run's marker in its environment
        (a child that detached or was re-parented to init is no longer in the tree
        but still has the marker)."""
        import psutil
        found = {}
        try:
            if process is not None and process.poll() is None:  # else its pid may already belong to someone else
                parent = psutil.Process(process.pid)
                for proc in [parent] + parent.children(recursive=True):
                    found[proc.pid] = proc
        except Exception:
            pass
        if token:
            me = os.getpid()
            for proc in psutil.process_iter():
                if proc.pid == me or proc.pid in found:
                    continue
                try:
                    if proc.environ().get(RUN_TOKEN_ENV) == token:
                        found[proc.pid] = proc
                except Exception:
                    continue  # gone meanwhile, or not ours to inspect
        alive = []
        for proc in found.values():
            try:
                if proc.status() != 'zombie':
                    alive.append(proc)
            except Exception:
                continue
        return alive

    def stop_run(self, process, token, grace=2.0, limit=10.0):
        """Stop a node run for good: SIGTERM to everything belonging to it, SIGKILL
        for whatever is still there after `grace` seconds, repeated until nothing
        is left or `limit` seconds are up (then say what could not be stopped)."""
        start = time.time()
        while True:
            victims = self._run_processes(process, token)
            if not victims:
                return
            elapsed = time.time() - start
            if elapsed > limit:
                names = ', '.join(f"{p.pid} ({' '.join(p.cmdline())[:60]})" for p in victims)
                print(f'[kwui] WARNING: could not stop: {names}', flush=True)
                return
            for proc in victims:
                try:
                    proc.kill() if elapsed >= grace else proc.terminate()
                except Exception:
                    pass
            time.sleep(0.2)

    def stop_leftovers(self, names):
        """Stop whatever earlier runs of nodes `names` left behind (a helper a finished
        step started and never ended, ...). Called when those nodes are about to run
        AGAIN, so a second copy can never pile up next to — or fight with — the first.
        Nothing is running at that moment, so everything found is a leftover."""
        tokens = [tok for tok, node in self._token_nodes.items() if node in names]
        for token in tokens:
            self.stop_run(None, token)
            self._token_nodes.pop(token, None)

    def await_stopped(self, timeout=15.0):
        """Wait until a user-requested stop (Reset) has really finished, so the
        step that restarts can never overlap with the one being stopped."""
        stopper = self._stopper
        if stopper is not None:
            stopper.join(timeout)
            self._stopper = None

    # -- node -> browser ----------------------------------------------------

    def handle_line(self, node_name, line):
        self._reading_token = self._live_token
        try:
            payload = line.split(KWUI_OPEN, 1)[1].rsplit(KWUI_CLOSE, 1)[0].strip()
            message = json.loads(payload)
        except Exception:
            return
        op = message.get('op')
        if op == 'create':
            self._create(message)
        elif op == 'create_child':
            self._create_child(message)
        elif op == 'call':
            self._call(message)
        elif op == 'app_call':
            self._app_call(message)
        elif op == 'clear':
            self._clear()
        elif op == 'end':
            status = message.get('status')
            if status == STATUS_JUMP and not self.request_jump(message.get('target'), 'node'):
                status = STATUS_ERROR  # unknown target: fail the node, loudly (message printed above)
            if status == STATUS_ERROR_SKIP:
                self._error_skipped.add(node_name)
            if message.get('detail'):
                self._details[node_name] = message['detail']
            self._statuses[node_name] = status

    def _create(self, message):
        if self._fresh:
            # Drop the "Working ..." placeholder the moment the node shows
            # something of its own.
            self._clear()
        widget_id = message.get('id')
        widget_type = message.get('type', '')
        args = [self._decode(widget_id, v) for v in (message.get('args') or [])]
        kwargs = {k: self._decode(widget_id, v)
                  for k, v in (message.get('kwargs') or {}).items()}
        factory = getattr(self._out, widget_type, None)
        if factory is None:
            print(f'[kwui] unknown widget type {widget_type!r}', flush=True)
            return
        try:
            widget = factory(*args, **kwargs)
        except Exception as exc:
            print(f'[kwui] cannot create {widget_type!r}: {exc}', flush=True)
            return
        self._widgets[widget_id] = widget
        if widget_type == 'columns':
            # UIWidgetRef.__getitem__/__iter__ address each column by the
            # deterministic id kwebui itself assigns (widget_id-c0, -c1, ...)
            # — register the real ColumnWidget objects kwebui already built
            # under those SAME ids, so a later create_child/call for
            # 'widget_id-cN' has something to find.
            for i, column in enumerate(widget):
                self._widgets[f'{widget_id}-c{i}'] = column

    def _create_child(self, message):
        """A container-like widget's `child_type(...)` call, from
        UIWidgetRef.__getattr__'s child_factory (see there for why this
        exists: the node picks the new id itself, since there's no return
        channel to learn kwebui's own internally-assigned one). Works for
        any widget whose real class already supports dynamic child creation
        via its own `__getattr__` (container, empty, sidebar, topbar,
        column) — anything else just doesn't have the attribute, so this
        fails the same way `_call` fails on an unknown method."""
        if self._fresh:
            self._clear()
        parent = self._widgets.get(message.get('parent'))
        if parent is None:
            return
        child_id = message.get('id')
        widget_type = message.get('type', '')
        args = [self._decode(child_id, v) for v in (message.get('args') or [])]
        kwargs = {k: self._decode(child_id, v)
                  for k, v in (message.get('kwargs') or {}).items()}
        factory = getattr(parent, widget_type, None)
        if factory is None:
            print(f"[kwui] {getattr(parent, 'widget_type', parent)} has no "
                  f'child type {widget_type!r}', flush=True)
            return
        try:
            self._widgets[child_id] = factory(*args, **kwargs)
        except Exception as exc:
            print(f'[kwui] cannot create child {widget_type!r}: {exc}', flush=True)

    def _call(self, message):
        widget = self._widgets.get(message.get('id'))
        if widget is None:
            return
        method_name = message.get('method', '')
        method = getattr(widget, method_name, None)
        if not callable(method):
            if method_name in ('__enter__', '__exit__'):
                # UIWidgetRef always tries these for `with self.ui.x(...):` —
                # a silent no-op here is exactly right for a widget type that
                # (unlike spinner/sidebar/topbar) has no context-manager
                # behavior of its own, matching how it would behave if you
                # just never entered a `with` block at all.
                return
            print(f"[kwui] {widget.widget_type} has no method "
                  f"{method_name!r}", flush=True)
            return
        args = [self._decode(message.get('id'), v) for v in (message.get('args') or [])]
        kwargs = {k: self._decode(message.get('id'), v)
                  for k, v in (message.get('kwargs') or {}).items()}
        try:
            method(*args, **kwargs)
        except Exception as exc:
            print(f"[kwui] {method_name!r} failed: {exc}", flush=True)

    def _app_call(self, message):
        """set_theme/on_shutdown/exit — see _APP_METHODS. Same shape as
        _call, just against self.app instead of a tracked widget."""
        if self.app is None:
            return
        method_name = message.get('method', '')
        method = getattr(self.app, method_name, None)
        if not callable(method):
            print(f"[kwui] KApp has no method {method_name!r}", flush=True)
            return
        args = [self._decode('__app__', v) for v in (message.get('args') or [])]
        kwargs = {k: self._decode('__app__', v)
                  for k, v in (message.get('kwargs') or {}).items()}
        try:
            method(*args, **kwargs)
        except Exception as exc:
            print(f"[kwui] KApp.{method_name} failed: {exc}", flush=True)

    def _clear(self):
        _stop_activity(self._out)
        self._out.clear()
        self._widgets.clear()
        self._fresh = False

    # -- browser -> node ----------------------------------------------------

    def _decode(self, widget_id, value):
        if isinstance(value, dict) and CB_MARKER in value:
            name = value[CB_MARKER]
            token = self._reading_token
            return lambda *args: self._send_event(widget_id, name, args, token)
        if isinstance(value, dict) and BYTES_MARKER in value:
            return _decode_bytes_value(value)
        return value

    def _send_event(self, widget_id, callback_name, args, token=None):
        payload = json.dumps({'op': 'event', 'id': widget_id,
                              'cb': callback_name, 'args': _jsonable(list(args))})
        with self._stdin_lock:
            stdin = self._stdin
            if stdin is None:
                return
            # A widget of an EARLIER node run (an old page, a previous REPEAT) must
            # never reach the node running now: widget ids are numbered per node
            # process, so the same id/callback key may exist in both.
            if token is not None and token != self._live_token:
                return
            try:
                stdin.write(payload + '\n')
                stdin.flush()
            except Exception:
                pass  # node already gone


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

WORKFLOW_FILE = 'workflow.py'

#: Same defaults the GUI server uses for its own port: all interfaces (so the
#: page is reachable from other machines), first free port at or above this one.
DEFAULT_HOST = '0.0.0.0'
DEFAULT_PORT = 8701


def workflow_path(pipeline_dir=None):
    return os.path.join(pipeline_dir or os.getcwd(), WORKFLOW_FILE)


def load_tasks(graph_config='graph_config.yaml'):
    """The tracker's step list: the UI nodes (`kwui: true`) of the pipeline's
    jump-link sequence, in order. Chain nodes without a UI still run but get
    no step. Called from workflow.py's module level."""
    return [{'id': name, 'title': name, 'status': 'pending'}
            for name in kwui_sequence(graph_config, ui_only=True)]


def kwui_sequence(graph_config='graph_config.yaml', ui_only=False):
    """Node names of the kwui run, in order. `ui_only=False` (default): the
    full execution sequence. `ui_only=True`: just the tracker steps."""
    from kpipeline.src.kgraph import KGraph
    graph = KGraph(verbose=False)
    graph.load_externals()
    graph.load_from_yaml(graph_config)
    sequence = graph.get_kwui_sequence()
    return tracker_steps(graph, sequence) if ui_only else sequence


def tracker_steps(graph, sequence):
    """The UI nodes (`kwui: true` in node_config.yaml) of `sequence`, in order —
    the only nodes the workflow tracker lists. The other nodes in the sequence
    still run; they just have no UI to show."""
    steps = []
    for name in sequence:
        node = graph.get_node(name)
        if node is not None and node.kwui:
            steps.append(name)
    return steps


def load_workflow_class(path=None):
    import importlib.util

    from kwebui import KApp

    path = path or workflow_path()
    spec = importlib.util.spec_from_file_location('kpl_kwui_workflow', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, KApp) and obj is not KApp:
            return obj
    raise RuntimeError(f'No KApp subclass found in {path}')


def _wait_for_browser(app, timeout=120):
    """Hold the pipeline until someone actually has the page open, otherwise a
    fast pipeline finishes before the first browser connects and the user only
    ever sees the final state. Says so on the terminal: an empty page plus a
    silent terminal is indistinguishable from a hung run."""
    if app._sessions:
        return
    print('Waiting for a browser to connect (open the URL above) ...', flush=True)
    deadline = time.monotonic() + timeout
    while not app._sessions and time.monotonic() < deadline:
        time.sleep(0.2)
    if not app._sessions:
        print(f'No browser connected within {timeout}s — running anyway; the '
              f'page will only show the final state.', flush=True)


def _find_free_port(host, start, tries=100):
    """First port at or above `start` that we can bind on `host` (same walk as
    the GUI server's 8601+).

    Resolved here rather than leaving it to kwebui's own drift so the URL we
    print and open is the one actually served. kwebui re-binds a moment later
    and would drift further if something snatched the port in between — then
    its own "Port X is in use" line is the authority.
    """
    for port in range(start, start + tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            return port
        except OSError:
            continue
    return None


def _lan_ip():
    """This machine's address on the LAN, for the "open it from your laptop"
    URL. Nothing is sent — connect() on UDP only picks a route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _open_when_ready(app, url, timeout=60):
    """Open the browser once the server actually accepts connections. `_loop` is
    set in the FastAPI lifespan hook, i.e. after the socket is listening — a
    fixed delay instead would open a dead tab on a loaded machine."""
    deadline = time.monotonic() + timeout
    while app._loop is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if app._loop is None:
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass  # headless machine / no browser — the printed URL is enough


def run_with_ui(run_one, sequence, *, path=None, host=DEFAULT_HOST,
                port=DEFAULT_PORT, open_browser=True, log_level='warning',
                invalidate=None, skip=None, accept=None):
    """Serve workflow.py's kwebui app and run `sequence` through `run_one`.

    `invalidate(names)` (optional) is called before steps `names` are run AGAIN
    after a jump / Reset, so the caller can forget their earlier results;
    `skip(names)` (optional) when a forward jump passes over steps `names`;
    `accept(name)` (optional) when the workflow goes on past a step that had failed.

    Blocks until interrupted: the server stays up after the pipeline finishes
    so the final tracker/container state remains visible. That is also why the
    port may be occupied on the next run — hence the loud message below.
    """
    app = load_workflow_class(path)()
    tracker = getattr(app, 'tracker', None)
    container = getattr(app, 'app_container', None)
    if tracker is None or container is None:
        raise RuntimeError(f'{WORKFLOW_FILE}: build() must set self.tracker '
                           f'(a workflow_tracker) and self.app_container (a container).')

    bridge = KwuiBridge(tracker, container, app)
    bridge.set_sequence(sequence)
    set_active_bridge(bridge)

    requested_port = port
    port = _find_free_port(host, requested_port)
    if port is None:
        print(f'No free port found in range {requested_port}–{requested_port + 99}.',
              flush=True)
        return
    if port != requested_port:
        # The likely cause: a previous `kpl run --kwui` whose server is still up
        # (it stays up on purpose). An already-open tab on the old port shows
        # THAT run — which looks exactly like this run's page never loading.
        print(f'\nPort {requested_port} is in use — probably a previous '
              f'`kpl run --kwui` still running. Serving this run on port {port}; '
              f'a tab still open on {requested_port} shows the OLD run.', flush=True)

    browsable_host = 'localhost' if host in ('0.0.0.0', '::') else host
    local_url = f'http://{browsable_host}:{port}'
    print(f'\nWorkflow UI → {local_url}', flush=True)
    if host in ('0.0.0.0', '::'):
        lan_ip = _lan_ip()
        if lan_ip:
            print(f'On the network → http://{lan_ip}:{port}', flush=True)

    def restart_at(target, current):
        """Index to continue at after a jump from step index `current`: forget the
        results of `target` and every later step, and put them back to pending in
        the tracker; steps jumped OVER (forward jump) are reported as skipped."""
        bridge.await_stopped()
        index = sequence.index(target)
        names = sequence[index:]
        print(f"\n[kwui] Continuing at step '{target}' ...", flush=True)
        stale = invalidate(names) if invalidate is not None else None
        bridge.stop_leftovers(set(names) | set(stale or ()))
        if index > current + 1 and skip is not None:
            skip(sequence[current + 1:index])
        bridge.reset_steps(names)
        return index

    def pipeline():
        _wait_for_browser(app)
        index = 0
        while True:
            failed = None
            while index < len(sequence):
                # A Reset that arrived between two steps.
                early = bridge.take_jump(True)
                if early == CONTINUE:      # a stale Continue (the workflow is running): nothing to do
                    early = None
                if early is not None:
                    index = restart_at(early, index)
                    continue
                name = sequence[index]
                bridge.begin_step(name)
                ok = run_one(name)
                # end('JUMP', ...) from the node, or a Reset that stopped it.
                target = bridge.take_jump(ok)
                if target == CONTINUE:
                    target = None
                if target is not None:
                    index = restart_at(target, index)
                    continue
                if not ok:
                    failed = name
                    break
                index += 1
            if failed is not None:
                print(f"\n[FAILED] Pipeline stopped at node '{failed}'.", flush=True)
            else:
                print('\n[OK] Pipeline finished. Press CTRL+C to stop the UI.', flush=True)
                if bridge.skipped_error_steps():
                    print('[kwui] Steps that ended with a skipped error (ERROR_SKIP): '
                          + ', '.join(bridge.skipped_error_steps()), flush=True)
            # The window stays open: Reset all / Restart step (or a late jump) start it
            # again, and Continue goes on past a step that stopped the workflow.
            bridge.set_stopped_on(failed)
            target = bridge.wait_for_jump()
            bridge.set_stopped_on(None)
            if target == CONTINUE and failed is not None:
                if accept is not None:
                    accept(failed)
                bridge.mark_continued(failed)
                index += 1
            else:
                index = restart_at(target, index)

    threading.Thread(target=pipeline, daemon=True).start()
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(app, local_url),
                         daemon=True).start()
    app.run(host=host, port=port, log_level=log_level)
