// ANSI escape codes → styled text pieces for the terminal panel.
//
// Node output carries the usual terminal colour codes (kpl itself colours the log
// level and the node name: knode.py _KPL_COLORS). parseAnsi() turns one line into
// [{ text, style }] pieces that TerminalPanel renders as React <span>s — never as
// HTML, so whatever a node prints can't inject markup into the GUI. SGR codes
// (colours, bold, dim, italic, underline, inverse) are applied; every other escape
// sequence (cursor moves, clear-line, OSC titles, ...) is dropped.

// The common "dark terminal" palette (VS Code's integrated terminal).
const BASE16 = [
  '#000000', '#cd3131', '#0dbc79', '#e5e510', '#2472c8', '#bc3fbc', '#11a8cd', '#e5e5e5',
  '#666666', '#f14c4c', '#23d18b', '#f5f543', '#3b8eea', '#d670d6', '#29b8db', '#ffffff',
]

function color256(n) {
  if (n < 16) return BASE16[n]
  if (n < 232) {   // 6×6×6 colour cube
    const levels = [0, 95, 135, 175, 215, 255]
    const i = n - 16
    return `rgb(${levels[Math.floor(i / 36)]},${levels[Math.floor(i / 6) % 6]},${levels[i % 6]})`
  }
  const g = 8 + (n - 232) * 10   // grayscale ramp
  return `rgb(${g},${g},${g})`
}

// CSI (ESC [ ... final byte), OSC (ESC ] ... BEL or ESC \), or any other ESC + one char.
// eslint-disable-next-line no-control-regex
const ESCAPE_RE = /\x1b\[([0-9;?]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]?/g

// eslint-disable-next-line no-control-regex
export const hasAnsi = text => /\x1b/.test(text)

export function stripAnsi(text) {
  return String(text).replace(ESCAPE_RE, '')
}

function applySgr(state, params) {
  const codes = params === '' ? [0] : params.split(';').map(p => (p === '' ? 0 : parseInt(p, 10)))
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i]
    if (c === 0) Object.keys(state).forEach(k => delete state[k])
    else if (c === 1) state.bold = true
    else if (c === 2) state.dim = true
    else if (c === 3) state.italic = true
    else if (c === 4) state.underline = true
    else if (c === 7) state.inverse = true
    else if (c === 22) { delete state.bold; delete state.dim }
    else if (c === 23) delete state.italic
    else if (c === 24) delete state.underline
    else if (c === 27) delete state.inverse
    else if (c >= 30 && c <= 37) state.fg = BASE16[c - 30]
    else if (c >= 90 && c <= 97) state.fg = BASE16[c - 90 + 8]
    else if (c >= 40 && c <= 47) state.bg = BASE16[c - 40]
    else if (c >= 100 && c <= 107) state.bg = BASE16[c - 100 + 8]
    else if (c === 39) delete state.fg
    else if (c === 49) delete state.bg
    else if (c === 38 || c === 48) {   // extended: 5;n (256 colours) or 2;r;g;b (true colour)
      const key = c === 38 ? 'fg' : 'bg'
      if (codes[i + 1] === 5 && codes[i + 2] !== undefined) {
        state[key] = color256(codes[i + 2] & 255); i += 2
      } else if (codes[i + 1] === 2 && codes[i + 4] !== undefined) {
        state[key] = `rgb(${codes[i + 2] & 255},${codes[i + 3] & 255},${codes[i + 4] & 255})`; i += 4
      }
    }
  }
}

function styleOf(state) {
  const s = {}
  let fg = state.fg, bg = state.bg
  if (state.inverse) {   // swap; "default" foreground/background = the panel's own colours
    fg = state.bg || '#1a202c'
    bg = state.fg || '#a0aec0'
  }
  if (fg) s.color = fg
  if (bg) s.backgroundColor = bg
  if (state.bold) s.fontWeight = 'bold'
  if (state.dim) s.opacity = 0.65
  if (state.italic) s.fontStyle = 'italic'
  if (state.underline) s.textDecoration = 'underline'
  return Object.keys(s).length ? s : null
}

// One line → [{ text, style }] (style null = the line's own colour).
export function parseAnsi(text) {
  const str = String(text ?? '')
  if (!hasAnsi(str)) return [{ text: str, style: null }]
  const pieces = []
  const state = {}
  let last = 0
  const push = chunk => {
    if (!chunk) return
    const style = styleOf(state)
    const prev = pieces[pieces.length - 1]
    if (prev && JSON.stringify(prev.style) === JSON.stringify(style)) prev.text += chunk
    else pieces.push({ text: chunk, style })
  }
  ESCAPE_RE.lastIndex = 0
  let m
  while ((m = ESCAPE_RE.exec(str)) !== null) {
    push(str.slice(last, m.index))
    if (m[2] === 'm' && !/\?/.test(m[1] || '')) applySgr(state, m[1] || '')
    last = ESCAPE_RE.lastIndex
  }
  push(str.slice(last))
  return pieces.length ? pieces : [{ text: '', style: null }]
}
