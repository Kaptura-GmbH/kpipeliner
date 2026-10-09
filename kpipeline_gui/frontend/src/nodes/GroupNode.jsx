import { memo, useState } from 'react'
import { Handle, Position, NodeResizer } from 'reactflow'
import './GroupNode.css'

// Per-handle visual overrides driven by the connected link's `handle_color`/
// `hide`/selection state — same scheme as IONode.jsx's handleVars() (kept as
// a separate copy since nothing here is exported/shared between the two node
// files), keyed the same way GraphPanel's Effect 1 builds these maps for
// EVERY node type, groups included ('src:<handle>' / 'tgt:<handle>').
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

// Group jump ports: execution-order-only, no data, one in / one out — a
// group is a jump router exactly like it already is for data ports. Each id
// plays two roles sharing it (see the GroupPort comment below for the same
// trick): 'jump-in' is a target on the OUTER edge (receives from a node
// outside the group) and a source just inside it (feeds a specific child's
// own 'jump-in'); 'jump-out' mirrors this — target on the inner side
// (accepts a child's jump-out), source on the outer edge (feeds a node
// outside). Hidden by default, revealed on hover/drag exactly like a node's
// jump handles (GraphPanel computes data._showJumpOut/_showJumpIn the same
// way for both node types).
//
// Curve direction (the same trick GroupPort uses, and just as easy to get
// backwards): a handle's `position` prop steers which way ReactFlow bends
// the bezier at that end — it is NOT the same thing as where the dot is
// drawn (that's the inline `style` left/right offset). Every TARGET handle
// here uses Position.Left and every SOURCE handle uses Position.Right,
// regardless of which visual side it sits on — an inner handle's dot sits
// near one border but its connection must bend the opposite way, into the
// group's interior, to reach a child. Flipping only the visual `style` and
// leaving `position` matching the dot's side (an earlier, wrong version of
// this file did that) draws a curve that bends the wrong way at that end.
//
// Second gotcha from the same position/style split: ReactFlow's own base
// CSS for a handle sets `left`/`right` from the `position` prop alone (e.g.
// Position.Left => left: 0). Whenever the inline `style` here places the dot
// on the OPPOSITE side from `position` (the two inner handles), that base
// `left`/`right` is still in effect underneath the inline style — and with
// width fixed, a left+right+width conflict always resolves in favor of
// `left`, silently discarding an inline `right` offset (an earlier version
// of this file hit exactly that: the inner jump-out dot collapsed onto the
// same spot as jump-in). The fix is the same one GroupPort already uses:
// whenever position and visual side differ, explicitly clear the side
// `position` implies (`left: 'auto'` / `right: 'auto'`) alongside the one
// that should actually win.
function GroupJumpPorts({ id, data, connected }) {
  // The hover/drag reveal signal is shared per pair (hovering the group
  // offers both of its "drag-from" or both of its "drop-onto" handles at
  // once) — but each handle's OWN connection state must gate independently,
  // or connecting just one of a pair (e.g. only the inner jump-out, from a
  // child) would leave its unrelated, still-unconnected sibling (the outer
  // jump-in) permanently visible too.
  const hoverOut = data._showJumpOut
  const hoverIn = data._showJumpIn
  const click = (side, handleId) => e => { e.stopPropagation(); data.onHandleClick?.(id, side, handleId) }
  return (
    <>
      {(hoverIn || connected.has('tgt:jump-in')) && (
        <Handle
          type="target" position={Position.Left} id="jump-in"
          className={`group-jump-handle group-jump-handle-in ${connected.has('tgt:jump-in') ? 'io-handle-connected' : ''}`}
          style={{ left: -5, ...handleVars(data, 'tgt:jump-in') }}
          title="Jump input: run this group after the dragged node finishes"
          onClick={click('tgt', 'jump-in')}
        />
      )}
      {(hoverOut || connected.has('src:jump-in')) && (
        <Handle
          type="source" position={Position.Right} id="jump-in"
          className={`group-jump-handle group-jump-handle-inner-in ${connected.has('src:jump-in') ? 'io-handle-connected' : ''}`}
          style={{ left: 6, right: 'auto', ...handleVars(data, 'src:jump-in') }}
          title="Jump pass-through: continues into a child node's jump input inside this group"
          onClick={click('src', 'jump-in')}
        />
      )}
      {(hoverIn || connected.has('tgt:jump-out')) && (
        <Handle
          type="target" position={Position.Left} id="jump-out"
          className={`group-jump-handle group-jump-handle-inner-out ${connected.has('tgt:jump-out') ? 'io-handle-connected' : ''}`}
          // right:26, not 6 — the fold button (⊟/⊞) sits in normal flex flow
          // at the header's right edge (~right:8-22px); a smaller offset here
          // renders this handle directly under it.
          style={{ right: 26, left: 'auto', ...handleVars(data, 'tgt:jump-out') }}
          title="Jump pass-through: accepts a jump output from a child node inside this group"
          onClick={click('tgt', 'jump-out')}
        />
      )}
      {(hoverOut || connected.has('src:jump-out')) && (
        <Handle
          type="source" position={Position.Right} id="jump-out"
          className={`group-jump-handle group-jump-handle-out ${connected.has('src:jump-out') ? 'io-handle-connected' : ''}`}
          style={{ right: -5, ...handleVars(data, 'src:jump-out') }}
          title="Jump output: run downstream node(s) after this group finishes"
          onClick={click('src', 'jump-out')}
        />
      )}
    </>
  )
}

// A user-defined group port routes across the group border, so it carries two
// handles with the same id: a target (outer side) that accepts the incoming
// link and a source (inner side) that continues it. ReactFlow keeps source and
// target handles in separate lists, so the shared id does not collide.
// The `position` prop of the inner handles points INTO the group (opposite to
// their visual side) — it steers the edge curve, and inner connections must
// bend toward the group interior. The dot placement itself comes from the
// inline left/right offsets, which win over the position class.
// Each dot renders filled when its side has a link, empty otherwise.
function GroupPort({ id, kind, name, connected, data = {} }) {
  const isInput = kind === 'input'
  const handleId = (isInput ? 'gin-' : 'gout-') + name
  const fillCls = has => (has ? 'group-port-filled' : 'group-port-empty')
  // Clicking a dot selects every link on THAT dot (GraphPanel's
  // handleHandleClick), exactly like a node's handle — each half of the
  // port (outer target / inner source) is its own handle with its own links.
  const click = side => e => { e.stopPropagation(); data.onHandleClick?.(id, side, handleId) }
  return (
    <>
      <Handle
        type="target"
        position={Position.Left}
        id={handleId}
        className={`group-port-handle ${fillCls(connected.has('tgt:' + handleId))}`}
        style={{ ...(isInput ? { left: -14 } : { right: -4, left: 'auto' }), ...handleVars(data, 'tgt:' + handleId) }}
        onClick={click('tgt')}
      />
      <Handle
        type="source"
        position={Position.Right}
        id={handleId}
        className={`group-port-handle ${fillCls(connected.has('src:' + handleId))}`}
        style={{ ...(isInput ? { left: -4, right: 'auto' } : { right: -14 }), ...handleVars(data, 'src:' + handleId) }}
        onClick={click('src')}
      />
      <span className="group-port-label">{name}</span>
    </>
  )
}

const GroupNode = memo(function GroupNode({ id, data, selected }) {
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(data.label || id)
  const folded = data.folded || false
  const color = data.color || '#2d4a6b'
  // disabled/highlight_color: code-only for now, no Properties-panel control
  // (see graph_config.yaml's groups[].disabled / groups[].highlight_color).
  const disabled = data.disabled || false
  const highlightColor = data.highlight_color || null

  const commitEdit = () => {
    setEditing(false)
    if (data.onTitleChange) data.onTitleChange(id, title)
  }

  const externalInputs = data.externalInputs || []
  const externalOutputs = data.externalOutputs || []
  const groupInputs = data.group_inputs || []
  const groupOutputs = data.group_outputs || []
  const connectedHandles = new Set(data.connectedHandles || [])

  // Both rings are computed here (not left as separate CSS class rules) so
  // they can coexist: an inline `style.boxShadow` fully replaces whatever a
  // CSS class would set for the same property rather than merging with it,
  // so a highlight ring living in its own class would silently disappear
  // the moment .group-node-selected's box-shadow class also applied.
  const ringShadows = []
  if (selected) ringShadows.push('0 0 0 1px #63b3ed')
  if (highlightColor) ringShadows.push(`0 0 0 3px ${highlightColor}`, `0 0 0 7px ${highlightColor}55`)

  return (
    <div
      className={`group-node ${selected ? 'group-node-selected' : ''} ${folded ? 'group-node-folded' : ''} ${data.extRunning ? 'io-node-ext-running' : ''}`}
      style={{
        borderColor: color,
        background: color + (folded ? '33' : '14'),
        ...(ringShadows.length ? { boxShadow: ringShadows.join(', ') } : {}),
      }}
    >
      {!folded && !disabled && (
        <NodeResizer
          minWidth={160}
          minHeight={80}
          isVisible={selected}
          color={color}
          onResizeEnd={(_, params) => data.onResize && data.onResize(id, params)}
        />
      )}

      <div className="group-header group-drag-handle" style={{ background: color, borderBottom: `1px solid ${color}` }}>
        <div className="group-title-block">
          {editing ? (
            <input
              autoFocus
              className="group-title-input"
              value={title}
              onChange={e => setTitle(e.target.value)}
              onBlur={commitEdit}
              onKeyDown={e => e.key === 'Enter' && commitEdit()}
            />
          ) : (
            <span className="group-title" onDoubleClick={e => { e.stopPropagation(); setEditing(true) }} title={data.description || undefined}>
              {folded ? '▶' : '▼'} {title}
            </span>
          )}
          <span className="group-id-label" title={id}>{id}</span>
        </div>
        <button
          className="group-fold-btn"
          onClick={() => data.onToggleFold && data.onToggleFold(id)}
          title={folded ? 'Expand group' : 'Fold to folder view'}
        >
          {folded ? '⊞' : '⊟'}
        </button>
        <GroupJumpPorts id={id} data={data} connected={connectedHandles} />
      </div>

      {/* Expanded view: user-defined ports pinned to the group border */}
      {!folded && groupInputs.map((name, i) => (
        <div key={name} className="group-port group-port-in" style={{ top: 42 + i * 22 }}>
          <GroupPort id={id} kind="input" name={name} connected={connectedHandles} data={data} />
        </div>
      ))}
      {!folded && groupOutputs.map((name, i) => (
        <div key={name} className="group-port group-port-out" style={{ top: 42 + i * 22 }}>
          <GroupPort id={id} kind="output" name={name} connected={connectedHandles} data={data} />
        </div>
      ))}

      {/* Folder view: user ports + auto-detected external handles */}
      {folded && (
        <div className="group-ext-handles">
          <div className="group-ext-col group-ext-inputs">
            {groupInputs.map(name => (
              <div key={'gin-' + name} className="group-ext-entry">
                <GroupPort id={id} kind="input" name={name} connected={connectedHandles} data={data} />
              </div>
            ))}
            {externalInputs.length === 0 && groupInputs.length === 0
              ? <span className="group-no-handles">no inputs</span>
              : externalInputs.map(h => (
                <div key={h.id} className="group-ext-entry">
                  <Handle type="target" position={Position.Left} id={'ext-in-' + h.id} className="group-handle" />
                  <span className="group-handle-label" title={'from ' + h.from}>{h.handle}</span>
                </div>
              ))
            }
          </div>
          <div className="group-ext-col group-ext-outputs">
            {groupOutputs.map(name => (
              <div key={'gout-' + name} className="group-ext-entry group-ext-entry-out">
                <GroupPort id={id} kind="output" name={name} connected={connectedHandles} data={data} />
              </div>
            ))}
            {externalOutputs.length === 0 && groupOutputs.length === 0
              ? <span className="group-no-handles">no outputs</span>
              : externalOutputs.map(h => (
                <div key={h.id} className="group-ext-entry group-ext-entry-out">
                  <span className="group-handle-label" title={'to ' + h.to}>{h.handle}</span>
                  <Handle type="source" position={Position.Right} id={'ext-out-' + h.id} className="group-handle" />
                </div>
              ))
            }
          </div>
        </div>
      )}

      {/* Disabled overlay: last child so it paints on top of everything
          else in this div (header, ports, folder view). Combined with the
          zIndex bump in GraphPanel.jsx (a disabled group renders ABOVE its
          children instead of behind them, the deliberate exception to the
          usual rule), this visually covers the group's contents and its
          pointer-events:auto blocks every click from reaching them or the
          header controls underneath - the group reads as fully inert. */}
      {disabled && <div className="group-disabled-overlay" title="Group disabled" />}
    </div>
  )
})

export default GroupNode
