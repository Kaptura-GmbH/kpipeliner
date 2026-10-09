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


def _status_of(msg):
    """The runner-facing status for an `end(msg)` argument: OKAY / ERROR /
    REPEAT when msg says so (case-insensitive), OKAY for any other message —
    arbitrary messages are data for `interact()`, not an outcome."""
    status = str(msg).strip().upper()
    return status if status in (STATUS_OKAY, STATUS_ERROR, STATUS_REPEAT) else STATUS_OKAY

# The node subprocess may run in its own venv without kwebui, so it cannot ask
# the registry which widgets exist. The parent puts the list in this env var
# instead — see KNode.execute(). Its purpose is to make a MISSPELLED call fail
# loudly: `self.ui` resolves unknown names as widget factories, so without this
# `self.ui.intract()` would just create a widget type nobody has and run on.
WIDGETS_ENV = 'KPL_KWUI_WIDGETS'

#: UIProxy's own methods — valid on `self.ui` but not widget names.
_UI_METHODS = ('interact', 'end', 'clear')

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

    def end(self, msg=STATUS_OKAY):
        """Report this node's UI outcome and release a waiting `interact()`,
        which returns `msg` unchanged.

        "OKAY" (the default) -> the node succeeded; "ERROR" -> fail the node
        even if it exits 0; "REPEAT" -> the runner re-executes this node once
        it exits (matched case-insensitively). Any OTHER message is just data
        for `interact()`'s caller — the node counts as succeeded.
        """
        status = _status_of(msg)
        self._message = msg
        self._send({'op': 'end', 'status': status})
        self._done.set()
        return status

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

    def end(self, msg=STATUS_OKAY):
        return _status_of(msg)


# ---------------------------------------------------------------------------
# Parent side — the bridge
# ---------------------------------------------------------------------------

_active_bridge = None


def get_active_bridge():
    return _active_bridge


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


class KwuiBridge:
    def __init__(self, tracker, container, app=None):
        self.tracker = tracker
        self.container = container
        self.app = app  # the KApp itself — only needed for _APP_METHODS calls
        self._widgets = {}
        self._stdin = None
        self._stdin_lock = threading.Lock()
        self._statuses = {}
        self._fresh = True

    # -- node lifecycle -----------------------------------------------------

    def on_node_state(self, node_name, state):
        if state == 'RUNNING':
            self._widgets.clear()
            self._fresh = True
            self._set_step(node_name, 'in-progress')
            self.container.clear()
            # Name the running node — a data dependency pulled in implicitly
            # (not a tracker step) can take a while (env install, heavy
            # compute); a content-free "Working ..." looks identical to a
            # hung page for as long as that takes.
            self.container.spinner(f'Working ... ({node_name})').update(active=True)
        elif state == 'COMPLETED':
            self._set_step(node_name, 'completed')
        elif state == 'FAILED':
            self._set_step(node_name, 'error')

    def _set_step(self, node_name, status):
        try:
            self.tracker.set_task_status(node_name, status)
        except ValueError:
            pass  # not a tracker step (a data dependency pulled in implicitly)

    def attach(self, node_name, stdin):
        with self._stdin_lock:
            self._stdin = stdin
        self._statuses.pop(node_name, None)

    def detach(self, node_name):
        with self._stdin_lock:
            self._stdin = None

    def consume_status(self, node_name):
        return self._statuses.pop(node_name, None)

    # -- node -> browser ----------------------------------------------------

    def handle_line(self, node_name, line):
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
            self._statuses[node_name] = message.get('status')

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
        factory = getattr(self.container, widget_type, None)
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
        self.container.clear()
        self._widgets.clear()
        self._fresh = False

    # -- browser -> node ----------------------------------------------------

    def _decode(self, widget_id, value):
        if isinstance(value, dict) and CB_MARKER in value:
            name = value[CB_MARKER]
            return lambda *args: self._send_event(widget_id, name, args)
        if isinstance(value, dict) and BYTES_MARKER in value:
            return _decode_bytes_value(value)
        return value

    def _send_event(self, widget_id, callback_name, args):
        payload = json.dumps({'op': 'event', 'id': widget_id,
                              'cb': callback_name, 'args': _jsonable(list(args))})
        with self._stdin_lock:
            stdin = self._stdin
            if stdin is None:
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
                port=DEFAULT_PORT, open_browser=True, log_level='warning'):
    """Serve workflow.py's kwebui app and run `sequence` through `run_one`.

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

    set_active_bridge(KwuiBridge(tracker, container, app))

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

    def pipeline():
        _wait_for_browser(app)
        for name in sequence:
            if not run_one(name):
                print(f"\n[FAILED] Pipeline stopped at node '{name}'.", flush=True)
                return
        print('\n[OK] Pipeline finished. Press CTRL+C to stop the UI.', flush=True)

    threading.Thread(target=pipeline, daemon=True).start()
    if open_browser:
        threading.Thread(target=_open_when_ready, args=(app, local_url),
                         daemon=True).start()
    app.run(host=host, port=port, log_level=log_level)
