import { memo, useState, useCallback } from 'react'
import { Handle, Position } from 'reactflow'
import './IONode.css'

// Per-handle visual overrides driven by the connected link's `handle_color`/
// `hide`/selection state (computed in GraphPanel, keyed like connectedHandles:
// 'src:<output>' / 'tgt:<input>'). CSS custom properties because .io-handle's
// colors are !important (needed to beat ReactFlow's own inline handle
// styles) — plain inline style properties can't win against that, but a var()
// fallback inside an !important rule can be overridden by setting the var.
function handleVars(data, key) {
  const color = data.handleColors?.[key]
  const hiddenRing = data.handleHighlights?.has(key)
  const selected = data.handleSelected?.has(key)
  if (!color && !hiddenRing && !selected) return undefined
  const vars = {}
  if (color) vars['--handle-center'] = color
  if (hiddenRing) vars['--handle-ring'] = color || '#f6ad55'
  const shadows = []
  if (hiddenRing) shadows.push(`0 0 0 3px ${color || '#f6ad55'}88`)
  if (selected) shadows.push('0 0 0 5px #7c63ed99')
  if (shadows.length) vars['--handle-glow'] = shadows.join(', ')
  return vars
}

const STATE_BORDER = {
  PENDING: '#4a5568',
  WAITING: '#b7791f',
  RUNNING: '#276749',
  COMPLETED: '#2b6cb0',
  FAILED: '#c53030',
  SKIPPED: '#2d3748',
}

const IONode = memo(function IONode({ id, data, selected }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(data.label)

  const state = data.state || 'PENDING'
  const borderColor = STATE_BORDER[state] || '#4a5568'
  // Pipeline input nodes get a brown title bar, output nodes a green one
  // (both -> split gradient); these markers win over the custom node color.
  const headerBg =
    data.is_input && data.is_output
      ? 'linear-gradient(90deg, #7c4a1e 0%, #7c4a1e 45%, #2e7d46 55%, #2e7d46 100%)'
      : data.is_input ? '#7c4a1e'
      : data.is_output ? '#2e7d46'
      : (data.color || '#2d4a6b')
  const connected = new Set(data.connectedHandles || [])

  const onDoubleClick = useCallback(() => setEditing(true), [])

  const commitEdit = useCallback(() => {
    setEditing(false)
    if (title !== data.label && data.onTitleChange) {
      data.onTitleChange(id, title)
    }
  }, [id, title, data])

  return (
    <div
      className={`io-node ${data.is_kwui ? 'io-node-kwui' : ''} ${selected ? 'io-node-selected' : ''} ${data.extRunning ? 'io-node-ext-running' : ''}`}
      style={{ borderColor }}
    >
      <div className="io-header" style={{ background: headerBg }}>
        <div className="io-header-top">
          {editing ? (
            <input
              autoFocus
              className="io-title-input"
              value={title}
              onChange={e => setTitle(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={e => { if (e.key === 'Enter') commitEdit() }}
            />
          ) : (
            <span className="io-title" onDoubleClick={onDoubleClick} title="Double-click to rename">
              {data.label}
            </span>
          )}
          {data.is_kwui && (
            <span className="io-kwui-badge" title="kwui-enabled: builds a web UI via self.ui in a `kpl run --kwui` run">
              UI
            </span>
          )}
          {data.frozen && (
            // Inline SVG, not the 🔒 emoji: emoji glyphs are raster (fixed-size
            // bitmap strikes on Linux), so they blur/pixelate under ReactFlow's
            // zoom transform. SVG stays crisp at every zoom level.
            <span className="io-frozen-badge" title="Cache frozen — protected from clear-cache operations">
              <svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
                <rect x="5" y="10.5" width="14" height="10" rx="2" fill="#f0c14b" />
                <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" fill="none" stroke="#f0c14b" strokeWidth="2.2" strokeLinecap="round" />
                <rect x="10.6" y="14" width="2.8" height="4" rx="1" fill="#1a202c" />
              </svg>
            </span>
          )}
          <span className={`io-state-dot state-dot-${state}`} title={state} />
        </div>
        {/* Jump ports: execution-order-only, no data. Not derived from the
            node type's declared inputs/outputs. Hidden by default; revealed
            transiently by GraphPanel hover/drag state (data._showJumpOut on
            plain hover, data._showJumpIn while dragging from a jump-out).
            Positioned on the title-bar edge, same x as regular output/input
            handles (right/left respectively). */}
        {(data._showJumpOut || connected.has('src:jump-out')) && (
          <Handle
            type="source"
            position={Position.Right}
            id="jump-out"
            className={`io-handle io-jump-handle io-jump-handle-out ${connected.has('src:jump-out') ? 'io-handle-connected' : ''}`}
            style={handleVars(data, 'src:jump-out')}
            title="Jump output: run downstream node(s) after this one finishes"
            onClick={e => { e.stopPropagation(); data.onHandleClick?.(id, 'src', 'jump-out') }}
          />
        )}
        {(data._showJumpIn || connected.has('tgt:jump-in')) && (
          <Handle
            type="target"
            position={Position.Left}
            id="jump-in"
            className={`io-handle io-jump-handle io-jump-handle-in ${connected.has('tgt:jump-in') ? 'io-handle-connected' : ''}`}
            style={handleVars(data, 'tgt:jump-in')}
            title="Jump input: run this node after the dragged node finishes"
            onClick={e => { e.stopPropagation(); data.onHandleClick?.(id, 'tgt', 'jump-in') }}
          />
        )}
        <div className="io-subtype">{data.node_type}</div>
      </div>

      <div className="io-body">
        {/* Inputs column */}
        <div className="io-inputs">
          {(data.inputs || []).map(name => (
            <div key={name} className="io-row io-row-input">
              <Handle
                type="target"
                position={Position.Left}
                id={name}
                className={`io-handle io-handle-left ${connected.has('tgt:' + name) ? 'io-handle-connected' : ''}`}
                style={handleVars(data, 'tgt:' + name)}
                onClick={e => { e.stopPropagation(); data.onHandleClick?.(id, 'tgt', name) }}
              />
              <span className="io-label">{name}</span>
            </div>
          ))}
        </div>

        {/* Outputs column */}
        <div className="io-outputs">
          {(data.outputs || []).map(name => (
            <div key={name} className="io-row io-row-output">
              <span className="io-label">{name}</span>
              <Handle
                type="source"
                position={Position.Right}
                id={name}
                className={`io-handle io-handle-right ${connected.has('src:' + name) ? 'io-handle-connected' : ''}`}
                style={handleVars(data, 'src:' + name)}
                onClick={e => { e.stopPropagation(); data.onHandleClick?.(id, 'src', name) }}
              />
            </div>
          ))}
        </div>
      </div>

      {/* Underlying-group chips: only BOUND slots render here (binding a
          slot to an actual group happens in the Properties panel, not
          here), one per line. Radio-like: selecting one deselects any other
          bound group on THIS node (GraphPanel.handleUnderlyingGroupClick
          enforces that, using the node id passed here) — clicking the
          already-selected one just deselects it, so none-selected is a
          valid state too. Selected: enabled + highlighted in its own color;
          deselected: disabled with no highlight (GroupNode.jsx renders
          that state). */}
      {(data.underlying_groups || []).filter(g => g.group_id).length > 0 && (
        <div className="io-underlying-groups">
          {data.underlying_groups.filter(g => g.group_id).map(g => (
            <span
              key={g.name}
              className={`io-group-chip ${g.enabled ? 'io-group-chip-enabled' : ''}`}
              style={{ '--chip-color': g.color || '#4a5568', '--chip-glow': (g.color || '#4a5568') + '99' }}
              onClick={e => { e.stopPropagation(); data.onUnderlyingGroupClick?.(id, g.group_id) }}
              title={`${g.label}${g.enabled ? ' (selected) — click to deselect' : ' — click to select'}`}
            >
              {g.label}
            </span>
          ))}
        </div>
      )}
    </div>
  )
})

export default IONode
