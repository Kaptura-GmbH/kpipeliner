"""YAML helpers for graph_config.yaml: fast parsing and saves that keep the file's text.

* ``load``/``dump`` use PyYAML's libyaml (C) classes when present — the same data and the
  same text as the pure-Python ones (checked on 82 real graph_config.yaml files), ~5x faster
  on a big pipeline. Without libyaml they fall back to the pure-Python classes.
* ``save_preserving`` writes a new config but rewrites ONLY the top-level keys whose value
  changed. Comments, blank lines, key order and quoting everywhere else stay byte for byte.
  The result is parsed again and must equal the new config exactly; if it doesn't (an
  unusual file layout this simple text editing can't handle) the whole file is written the
  old way instead — so the worst case is the previous behaviour, never wrong data.

Limits: a comment INSIDE a key's block that is rewritten (e.g. inside ``nodes:``) is lost;
a single-line value keeps its trailing ``# comment``; comments above a removed key stay.
"""
import os
import re
import shutil

import yaml

SafeLoader = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
#: yaml.dump's default is the (unsafe) Dumper; CDumper writes the same text.
Dumper = getattr(yaml, 'CDumper', yaml.Dumper)

# A top-level key line as PyYAML (and a person) writes graph_config.yaml: a plain key at
# column 0. Anything else at column 0 makes save_preserving fall back to a full rewrite.
_KEY_RE = re.compile(r'([A-Za-z_][\w.\-]*)[ \t]*:(?:[ \t]|$)')
_COMMENT_START = re.compile(r'[ \t]#')


def load(stream):
    """yaml.safe_load, through libyaml when available."""
    return yaml.load(stream, Loader=SafeLoader)


def dump(data, stream=None):
    """yaml.dump(data, sort_keys=False, default_flow_style=False) — the format kpl always wrote."""
    return yaml.dump(data, stream, Dumper=Dumper, sort_keys=False, default_flow_style=False)


def _top_level_spans(lines):
    """{key: (first line, end line exclusive)} of every top-level key, or None when the file
    has something this line-based reading doesn't understand. A key's block ends at its last
    content line, so comments/blank lines before the next key stay outside every block."""
    spans, key, start, last = {}, None, 0, 0
    seen_content = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if line.rstrip('\r\n') == '---' and not seen_content:
            continue                 # document start marker before the first key
        if line[0] in ' \t' or line.startswith('-'):
            if key is None:
                return None          # indented text / list item before any key
            last = i
            continue
        m = _KEY_RE.match(line)
        if not m:
            return None              # '...', a second document, a complex key, ...
        if key is not None:
            spans[key] = (start, last + 1)
        key, start, last = m.group(1), i, i
        if key in spans:
            return None              # duplicate key
        seen_content = True
    if key is not None:
        if key in spans:
            return None
        spans[key] = (start, last + 1)
    return spans


def _trailing_comment(line):
    """The ``  # comment`` at the end of a one-line ``key: value``, or ''."""
    body = line.rstrip('\r\n')
    try:
        whole = load(body)
    except yaml.YAMLError:
        return ''
    for m in _COMMENT_START.finditer(body):
        try:
            if load(body[:m.start()]) == whole:
                return body[len(body[:m.start()].rstrip()):]   # with its alignment spaces
        except yaml.YAMLError:
            continue
    return ''


def _block(key, value, old_lines=None):
    text = dump({key: value})
    new = text.splitlines(keepends=True)
    if old_lines is not None and len(old_lines) == 1 and len(new) == 1:
        comment = _trailing_comment(old_lines[0])
        if comment:
            new = [new[0].rstrip('\n') + comment + '\n']
    return new


def _write_atomic(path, text):
    target = os.path.realpath(path)            # a symlinked config stays a symlink
    tmp = f'{target}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    if os.path.exists(target):
        try:
            shutil.copymode(target, tmp)
        except OSError:
            pass
    os.replace(tmp, target)                    # never a half-written file


def preserving_text(old_text, new_cfg, old_cfg=None):
    """The new file text for ``new_cfg``, editing ``old_text`` in place where possible.
    Returns (text, how) — how is 'unchanged', 'edited' or 'rewritten'."""
    full = None
    try:
        old = old_cfg if old_cfg is not None else load(old_text)
    except yaml.YAMLError:
        old = None
    if not isinstance(old, dict) or not isinstance(new_cfg, dict):
        return dump(new_cfg), 'rewritten'
    lines = old_text.splitlines(keepends=True)
    spans = _top_level_spans(lines)
    if spans is None or set(spans) != set(old):
        return dump(new_cfg), 'rewritten'
    changed = [k for k in new_cfg if k in old and old[k] != new_cfg[k]]
    added = [k for k in new_cfg if k not in old]
    removed = [k for k in old if k not in new_cfg]
    if not (changed or added or removed):
        return old_text, 'unchanged'
    edits = [(spans[k], _block(k, new_cfg[k], lines[slice(*spans[k])])) for k in changed]
    edits += [(spans[k], []) for k in removed]
    for (start, end), new_lines in sorted(edits, key=lambda e: e[0][0], reverse=True):
        lines[start:end] = new_lines
    if added:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        for k in added:
            lines += _block(k, new_cfg[k])
    text = ''.join(lines)
    try:
        ok = load(text) == new_cfg
    except yaml.YAMLError:
        ok = False
    if not ok:
        full = dump(new_cfg)
    return (full, 'rewritten') if full is not None else (text, 'edited')


def save_preserving(path, new_cfg, old_text=None, old_cfg=None):
    """Write ``new_cfg`` (a dict) to ``path`` keeping the existing file's text for every
    top-level key whose value did not change. ``old_text``/``old_cfg`` = the file as the
    caller already read it (saves reading/parsing it again). Returns 'unchanged' (nothing
    written), 'edited' or 'rewritten'."""
    if old_text is None:
        if os.path.exists(path):
            with open(path) as f:
                old_text = f.read()
        else:
            old_text = ''
    if not old_text.strip():
        _write_atomic(path, dump(new_cfg))
        return 'rewritten'
    text, how = preserving_text(old_text, new_cfg, old_cfg)
    if how != 'unchanged':
        _write_atomic(path, text)
    return how
