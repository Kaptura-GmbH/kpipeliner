import { useCallback, useRef, useState, useEffect, useMemo } from 'react'
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  useReactFlow,
  ReactFlowProvider,
} from 'reactflow'
import IONode from '../nodes/IONode.jsx'
import GroupNode from '../nodes/GroupNode.jsx'
import { compareCategories } from './ComponentPanel.jsx'

const nodeTypes = {
  ioNode: IONode,
  groupNode: GroupNode,
}

const REF_RE = /^nodes\.\w+\.\w+$/
const EMPTY_HANDLE_COLORS = {}
const EMPTY_HANDLE_SET = new Set()

// ── Context menu ───────────────────────────────────────────────────────────
function ContextMenu({ x, y, nodeId, ioFlags, frozen, nodeTypes: ntypes, onClose, onAddNode, onDeleteNode, onRunNode, onAddGroup, onDuplicateNode, onRemoveFromGroup, onToggleIOFlag }) {
  const [expandedCat, setExpandedCat] = useState(null)
  const cats = {}
  ntypes.forEach(nt => {
    const cat = nt.category || 'General'
    if (!cats[cat]) cats[cat] = []
    cats[cat].push(nt)
  })
  return (
    <>
      <div style={{ position: 'fixed', inset: 0, zIndex: 9997 }} onClick={onClose} />
      <div className="ctx-menu" style={{ top: y, left: x, zIndex: 9998 }}>
        {nodeId ? (
          <>
            <div className="ctx-menu-label">Node: {nodeId}</div>
            <div className="ctx-menu-item" onClick={() => { onRunNode(nodeId); onClose() }}>▶ Run this node</div>
            <div className="ctx-menu-item" onClick={() => { onDuplicateNode(nodeId); onClose() }}>⧉ Duplicate</div>
            {ioFlags && (
              <>
                <div className="ctx-menu-item" onClick={() => { onToggleIOFlag(nodeId, 'is_input'); onClose() }}>
                  <span style={{ color: '#c98a4b' }}>●</span> {ioFlags.isInput ? 'Remove from inputs list' : 'Add to inputs list'}
                </div>
                <div className="ctx-menu-item" onClick={() => { onToggleIOFlag(nodeId, 'is_output'); onClose() }}>
                  <span style={{ color: '#48bb78' }}>●</span> {ioFlags.isOutput ? 'Remove from outputs list' : 'Add to outputs list'}
                </div>
              </>
            )}
            {onRemoveFromGroup && (
              <div className="ctx-menu-item" onClick={() => { onRemoveFromGroup(nodeId); onClose() }}>⊠ Remove from group</div>
            )}
            {typeof frozen === 'boolean' && (
              <div className="ctx-menu-item" onClick={() => { onToggleIOFlag(nodeId, 'frozen'); onClose() }}>
                {frozen ? '🔓 Unfreeze cache' : '🔒 Freeze cache'}
              </div>
            )}
            <div style={{ height: 1, background: '#4a5568', margin: '2px 0' }} />
            <div className="ctx-menu-item danger" onClick={() => { onDeleteNode(nodeId); onClose() }}>✕ Delete node</div>
          </>
        ) : (
          <>
            <div className="ctx-menu-item" onClick={() => { onAddGroup(); onClose() }}>⊞ Add Group</div>
            <div style={{ height: 1, background: '#4a5568', margin: '2px 0' }} />
            <div className="ctx-menu-label">Add Node</div>
            {Object.entries(cats).sort(([a], [b]) => compareCategories(a, b)).map(([cat, nodes]) => (
              <div key={cat}>
                <div className="ctx-menu-item ctx-category-item"
                  onClick={() => setExpandedCat(p => p === cat ? null : cat)}>
                  <span className="ctx-arrow">{expandedCat === cat ? '▼' : '▶'}</span>
                  <span>{cat}</span>
                  <span style={{ marginLeft: 'auto', color: '#4a5568', fontSize: 10 }}>{nodes.length}</span>
                </div>
                {expandedCat === cat && nodes.map(n => (
                  <div key={n.type} className="ctx-menu-item ctx-node-entry" title={n.description}
                    onClick={() => { onAddNode(n.type); onClose() }}>
                    ◈ {n.type}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
      </div>
    </>
  )
}

// ── Helpers ────────────────────────────────────────────────────────────────
function pointInGroup(px, py, group) {
  const gx = group.position.x, gy = group.position.y
  const gw = group.style?.width || 360, gh = group.style?.height || 280
  return px >= gx && px <= gx + gw && py >= gy && py <= gy + gh
}

function computeGroupHandles(groupId, allNodes, allEdges) {
  const childIds = new Set(allNodes.filter(n => n.parentNode === groupId).map(n => n.id))
  const inputs = [], outputs = []
  const seenIn = new Set(), seenOut = new Set()
  allEdges.forEach(e => {
    // Edges to/from the group's own user ports are explicit routing,
    // not auto-detected external connections.
    if (e.source === groupId || e.target === groupId) return
    // Jump edges are an execution-order-only annotation, not a group port
    // routing chain — never fold them into the group's external ports.
    if (e.sourceHandle === 'jump-out' || e.targetHandle === 'jump-in') return
    if (childIds.has(e.target) && !childIds.has(e.source)) {
      const key = `${e.target}__${e.targetHandle}`
      if (!seenIn.has(key)) { seenIn.add(key); inputs.push({ id: key, handle: e.targetHandle, from: `${e.source} · ${e.sourceHandle}` }) }
    } else if (childIds.has(e.source) && !childIds.has(e.target)) {
      const key = `${e.source}__${e.sourceHandle}`
      if (!seenOut.has(key)) { seenOut.add(key); outputs.push({ id: key, handle: e.sourceHandle, to: `${e.target} · ${e.targetHandle}` }) }
    }
  })
  return { inputs, outputs }
}

// ── Inner graph ────────────────────────────────────────────────────────────
function GraphInner({
  nodes: extNodes, edges: extEdges,
  nodeStates, externalRunning, nodeTypes: ntypes,
  onGraphChange, onSelectNode, onSelectEdges,
  selectedNode, selectedEdges, onNodeRename,
  onRunNode, onDeleteNode,
  onAddGroupNode, onAddNodeAtPosition,
  onToggleIOFlag,
  guiConfig,
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState([])
  const [edges, setEdges, onEdgesChange] = useEdgesState([])
  const [ctxMenu, setCtxMenu] = useState(null)
  // Jump-port reveal state: a node's jump-out shows on plain hover; while
  // dragging a connection from a jump-out, the hovered *other* node reveals
  // its jump-in instead. Transient UI state only, never persisted.
  const [hoveredNodeId, setHoveredNodeId] = useState(null)
  const [jumpDragFrom, setJumpDragFrom] = useState(null)
  const reactFlowWrapper = useRef(null)
  const { project } = useReactFlow()

  // Always-fresh refs — safe to read inside callbacks & effects
  const extNodesRef = useRef(extNodes)
  const extEdgesRef = useRef(extEdges)
  extNodesRef.current = extNodes
  extEdgesRef.current = extEdges

  // Local RF state also needs a fresh ref — it (not extNodes/extEdges) is
  // where the `.selected` flag actually lives, needed by the custom Delete
  // key handler below.
  const localNodesRef = useRef(nodes)
  const localEdgesRef = useRef(edges)
  localNodesRef.current = nodes
  localEdgesRef.current = edges

  // App-level selection, mirrored into a ref for the same reason — read
  // inside a stable callback (handleHandleClick) below.
  const selectedEdgesRef = useRef(selectedEdges)
  selectedEdgesRef.current = selectedEdges

  // Stable callbacks (don't close over stale extNodes/extEdges)
  const handleToggleFold = useCallback((id) => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    onGraphChange(nds.map(n => n.id === id ? { ...n, data: { ...n.data, folded: !n.data.folded } } : n), eds)
  }, [onGraphChange])

  const handleTitleChange = useCallback((id, label) => onNodeRename(id, label), [onNodeRename])

  const handleGroupResize = useCallback((id, dims) => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    onGraphChange(nds.map(n => n.id === id
      ? { ...n, position: { x: dims.x, y: dims.y }, style: { ...n.style, width: dims.width, height: dims.height } }
      : n), eds)
  }, [onGraphChange])

  // A chip in a node's underlying_groups list (IONode.jsx) — only ever
  // rendered for an already-BOUND slot (binding itself happens in the
  // Properties panel). Clicking selects/deselects the bound group: selected
  // -> enabled + highlighted in the group's own color; deselected -> back to
  // disabled with no highlight. Radio-like across ALL of nodeId's OWN bound
  // slots (not global): selecting one deselects any other bound group on
  // the SAME node that was enabled; deselecting the current one just turns
  // it off (none-selected is a valid state, not just exactly-one). Same
  // generic node-array-patch pattern as handleToggleFold above, just
  // potentially touching several OTHER nodes (the groups) at once.
  const handleUnderlyingGroupClick = useCallback((nodeId, groupId) => {
    if (!groupId) return
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const callingNode = nds.find(n => n.id === nodeId)
    const siblingIds = new Set(
      (callingNode?.data?.underlying_groups || []).map(s => s.group_id).filter(Boolean)
    )
    const target = nds.find(n => n.id === groupId)
    const nowSelecting = Boolean(target?.data?.disabled)
    onGraphChange(nds.map(n => {
      if (n.id === groupId) {
        return { ...n, data: { ...n.data, disabled: !nowSelecting, highlight_color: nowSelecting ? (n.data.color || null) : null } }
      }
      if (nowSelecting && n.id !== groupId && siblingIds.has(n.id)) {
        // Selecting a new one: force off any other bound sibling that was on.
        return { ...n, data: { ...n.data, disabled: true, highlight_color: null } }
      }
      return n
    }), eds)
  }, [onGraphChange])

  // "Enabled" is meant to be a transient focus, not a sticky toggle: an
  // enabled group's scope is itself, its own children, and its "parent"
  // (whichever node bound it via underlying_groups) — clicking anything
  // else disables it. Shared by onNodeClick and onPaneClick below.
  const disableGroupsOutsideScope = useCallback((clickedNodeId) => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const clickedNode = clickedNodeId ? nds.find(n => n.id === clickedNodeId) : null
    const enabledGroups = nds.filter(n => n.type === 'groupNode' && n.data?.disabled === false && n.data?.highlight_color)
    const isInScope = (group) => {
      if (!clickedNode) return false // pane click - always outside every group's scope
      if (clickedNode.id === group.id) return true
      if (clickedNode.parentNode === group.id) return true
      return (clickedNode.data?.underlying_groups || []).some(s => s.group_id === group.id)
    }
    const toDisable = enabledGroups.filter(g => !isInScope(g))
    if (toDisable.length === 0) return
    const disableIds = new Set(toDisable.map(g => g.id))
    onGraphChange(nds.map(n => disableIds.has(n.id)
      ? { ...n, data: { ...n.data, disabled: true, highlight_color: null } }
      : n), eds)
  }, [onGraphChange])

  const onPaneClickWithGroupFocus = useCallback(() => {
    setCtxMenu(null)
    disableGroupsOutsideScope(null)
  }, [disableGroupsOutsideScope])

  const onNodeClick = useCallback((_event, node) => {
    // Clicking a DISABLED group selects its "parent" (calling) node instead
    // — the node that bound it via underlying_groups — so the user can find
    // who owns it. A disabled group's content is otherwise fully inert
    // (GroupNode.jsx's overlay); this is the one interaction left on it.
    if (node.type === 'groupNode' && node.data?.disabled) {
      const nds = extNodesRef.current
      const parent = nds.find(n => (n.data?.underlying_groups || []).some(s => s.group_id === node.id))
      if (parent) {
        setNodes(prev => prev.map(n => ({ ...n, selected: n.id === parent.id })))
      }
    }
    disableGroupsOutsideScope(node.id)
  }, [disableGroupsOutsideScope])

  // Double-clicking a group on the canvas is a second way to activate it —
  // same effect as selecting it from its owning node's chip list
  // (handleUnderlyingGroupClick above): enabled + highlighted, with any
  // other bound sibling on the SAME node forced off (radio-exclusive).
  // Deliberately always-enable (not a toggle like the chip click) rather
  // than reusing handleUnderlyingGroupClick directly: double-clicking a
  // node that's already selected is where a real browser/ReactFlow quirk
  // around delegated dblclick dispatch lives (observed empirically — the
  // native 'dblclick' DOM event fires, but doesn't reliably reach this
  // handler every time for an already-selected node). An idempotent
  // "always enable" sidesteps that entirely: if the unreliable case means
  // this handler sometimes doesn't fire on a repeat double-click, nothing
  // is wrong, since the group was already in the desired (enabled) state
  // from the first one. Only applies to a group actually bound as an
  // underlying_groups slot on some node — a plain, unrelated group has no
  // sibling list to manage, so this is a no-op for it (GroupNode's title
  // keeps its own, unrelated double-click meaning — renaming — via its own
  // stopPropagation).
  const onNodeDoubleClick = useCallback((_event, node) => {
    if (node.type !== 'groupNode') return
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const parent = nds.find(n => (n.data?.underlying_groups || []).some(s => s.group_id === node.id))
    if (!parent) return
    const siblingIds = new Set((parent.data?.underlying_groups || []).map(s => s.group_id).filter(Boolean))
    onGraphChange(nds.map(n => {
      if (n.id === node.id) {
        return { ...n, data: { ...n.data, disabled: false, highlight_color: n.data.color || null } }
      }
      if (n.id !== node.id && siblingIds.has(n.id)) {
        return { ...n, data: { ...n.data, disabled: true, highlight_color: null } }
      }
      return n
    }), eds)
  }, [onGraphChange])

  // Clicking a connected handle selects every link attached to it — the
  // only way to (re)select a HIDDEN link, which has no visible path to
  // click, but works the same way for a visible one too (a quick way to
  // select a link without hunting for its curve). A fan-out/fan-in handle
  // (e.g. an output feeding several nodes, or a jump-in fed by several) has
  // more than one link on it — all of them get selected together, so the
  // Properties panel can show/edit them as a group (handle_color is genuinely
  // shared across a handle's links; see App.jsx's handleEdgeUpdate). Clicking
  // the same handle again toggles the whole group back off. Bypasses
  // ReactFlow's own selection machinery entirely, so it also has to mirror
  // what onSelectionChange normally does: clear node selection and mark the
  // edges selected in the local RF state (so they stay visually selected if
  // a link is later hidden/un-hidden).
  const handleHandleClick = useCallback((nodeId, side, handleId) => {
    const matches = extEdgesRef.current.filter(e => (
      side === 'src'
        ? (e.source === nodeId && e.sourceHandle === handleId)
        : (e.target === nodeId && e.targetHandle === handleId)
    ))
    if (matches.length === 0) return
    const matchIds = new Set(matches.map(e => e.id))
    const current = selectedEdgesRef.current || []
    const deselecting = current.length === matchIds.size && current.every(e => matchIds.has(e.id))
    setNodes(nds => nds.map(n => (n.selected ? { ...n, selected: false } : n)))
    setEdges(eds => eds.map(e => ({ ...e, selected: !deselecting && matchIds.has(e.id) })))
    onSelectNode(null)
    onSelectEdges(deselecting ? [] : matches)
  }, [onSelectNode, onSelectEdges, setNodes, setEdges])

  // ─────────────────────────────────────────────────────────────────────────
  // Derived keys that change ONLY when group structure (fold/parent) changes.
  // Used as edge-effect deps so we don't call setEdges on plain node moves.
  // ─────────────────────────────────────────────────────────────────────────
  const foldedKey = useMemo(
    () => extNodes.filter(n => n.type === 'groupNode' && n.data?.folded).map(n => n.id).sort().join(','),
    [extNodes]
  )
  const parentKey = useMemo(
    () => extNodes.filter(n => n.parentNode).map(n => `${n.id}:${n.parentNode}`).sort().join(','),
    [extNodes]
  )

  // ── Effect 1: NODE sync ───────────────────────────────────────────────────
  // Runs on any structural or state change.
  // Uses functional setNodes(prev=>) to preserve ReactFlow's selection flag.
  // NEVER calls setEdges — so plain node moves don't touch edge objects.
  useEffect(() => {
    const foldedGroups = new Set(extNodes.filter(n => n.type === 'groupNode' && n.data?.folded).map(n => n.id))
    // hidden/disabled: code-only for now (no Properties-panel control) - set
    // via graph_config.yaml's groups[].hidden/disabled. A hidden node is
    // never mounted by React Flow, which also stops any edge touching it
    // from rendering (no extra edge-hiding code needed here, unlike fold's
    // redirect-to-folder-view handling below - hiding has nothing to
    // redirect to). Disabled just marks state for the render below.
    const hiddenGroups = new Set(extNodes.filter(n => n.type === 'groupNode' && n.data?.hidden).map(n => n.id))
    const disabledGroups = new Set(extNodes.filter(n => n.type === 'groupNode' && n.data?.disabled).map(n => n.id))
    // For enriching a node's underlying_groups chips with their bound
    // target's LIVE color/enabled state — structural data (name/group_id/
    // label) comes from the server; only GraphPanel has every group's
    // current data available to cross-reference, the same reason
    // computeGroupHandles below takes extNodes rather than being computed
    // server-side.
    const groupById = new Map(extNodes.filter(n => n.type === 'groupNode').map(n => [n.id, n]))

    // Nodes running in a terminal `kpl run` (same workdir) get a glow. A
    // folded group glows in place of its hidden running children.
    const extRunSet = new Set(externalRunning || [])

    // Which handles have at least one edge — handles render filled when
    // connected, empty when not ('src:'/'tgt:' prefix keeps the sides apart).
    // Same pass also collects per-handle `handle_color`/`hide` overrides (set
    // via the Properties panel when a link is selected) and which handle(s)
    // belong to the currently selected link — all keyed the same way, so
    // IONode can look every one of them up by the identical 'src:'/'tgt:' key.
    const connectedByNode = {}
    const handleColorByNode = {}
    const handleHiddenByNode = {}
    const handleSelectedByNode = {}
    extEdges.forEach(e => {
      const srcKey = 'src:' + (e.sourceHandle ?? '')
      const tgtKey = 'tgt:' + (e.targetHandle ?? '')
      ;(connectedByNode[e.source] = connectedByNode[e.source] || []).push(srcKey)
      ;(connectedByNode[e.target] = connectedByNode[e.target] || []).push(tgtKey)
      const color = e.data?.handle_color
      if (color) {
        ;(handleColorByNode[e.source] = handleColorByNode[e.source] || {})[srcKey] = color
        ;(handleColorByNode[e.target] = handleColorByNode[e.target] || {})[tgtKey] = color
      }
      if (e.data?.hide) {
        ;(handleHiddenByNode[e.source] = handleHiddenByNode[e.source] || new Set()).add(srcKey)
        ;(handleHiddenByNode[e.target] = handleHiddenByNode[e.target] || new Set()).add(tgtKey)
      }
    })
    ;(selectedEdges || []).forEach(se => {
      const srcKey = 'src:' + (se.sourceHandle ?? '')
      const tgtKey = 'tgt:' + (se.targetHandle ?? '')
      ;(handleSelectedByNode[se.source] = handleSelectedByNode[se.source] || new Set()).add(srcKey)
      ;(handleSelectedByNode[se.target] = handleSelectedByNode[se.target] || new Set()).add(tgtKey)
    })

    const mapped = extNodes.map(n => {
      const hidden = Boolean(n.parentNode && (foldedGroups.has(n.parentNode) || hiddenGroups.has(n.parentNode))) ||
        (n.type === 'groupNode' && hiddenGroups.has(n.id))
      const parentDisabled = Boolean(n.parentNode && disabledGroups.has(n.parentNode))
      let externalInputs = [], externalOutputs = []
      if (n.type === 'groupNode') {
        const { inputs, outputs } = computeGroupHandles(n.id, extNodes, extEdges)
        externalInputs = inputs
        externalOutputs = outputs
      }
      // React Flow applies style.width/height to the node WRAPPER, which is
      // also the drag hit area — a folded group must shrink physically, not
      // just visually, so drop the expanded size while folded. The original
      // style stays untouched in the external nodes (only `position` is
      // synced back on drag), so unfolding restores the full size.
      const style = n.type === 'groupNode' && n.data?.folded
        ? { ...n.style, width: undefined, height: undefined }
        : n.style
      const isDisabledGroup = n.type === 'groupNode' && disabledGroups.has(n.id)
      // Enrich this node's declared group slots (structural: name/group_id/
      // label, from the server) with their bound target's LIVE color/
      // enabled state, for the under-node chip list (IONode.jsx) — an
      // unbound slot (group_id null) stays as-is, IONode filters those out.
      const underlyingGroups = (n.data?.underlying_groups || []).map(slot => {
        const target = slot.group_id ? groupById.get(slot.group_id) : null
        // label recomputed fresh too, not just color/enabled — a slot just
        // bound via the Properties panel only ever sets group_id (the one
        // thing that's actually persisted), so trusting a label written
        // there would show blank until the next full reload re-resolved it
        // server-side. This is the single source of truth for all three.
        return target
          ? { ...slot, label: target.data?.label || target.id, color: target.data?.color, enabled: !target.data?.disabled }
          : slot
      })
      return {
        ...n,
        style,
        hidden,
        // -2000, not -1: ReactFlow's elevateNodesOnSelect (default true, not
        // overridden on <ReactFlow> below) adds +1000 to a SELECTED node's
        // effective z internally — selecting/dragging/resizing a group would
        // otherwise push it (e.g. -1+1000=999) above an unselected child
        // node (1), which is exactly the "group renders on top of its own
        // nodes" bug. -2000 keeps it behind even a normal node that is
        // itself selected (1+1000=1001). A DISABLED group is the deliberate
        // exception: its hatch overlay (GroupNode.jsx) exists specifically
        // to visually cover its children and block interaction with them,
        // so it needs to render ON TOP instead - 6000 clears even a
        // selected normal node (1001). Its CHILDREN get 5000 here, but React
        // Flow actually CLAMPS a child's rendered z up to at least its
        // parent's (confirmed against the installed source) regardless of
        // what's set here - so a disabled group and its children always end
        // up TIED at 6000 in practice. That tie is exactly why array order
        // matters (see the .sort() below this map()): among tied elements
        // React Flow's stacking follows array order, and a freshly created
        // child gets APPENDED after its already-present parent group in
        // that array - rendering it ABOVE the hatch until a full reload
        // happened to reorder things correctly. The sort fixes the real
        // cause; this 5000 is now mostly documentation of intent.
        zIndex: isDisabledGroup ? 6000 : (n.type === 'groupNode' ? -2000 : (parentDisabled ? 5000 : 1)),
        // Groups move only by their titlebar (.group-drag-handle in
        // GroupNode.jsx) — dragging the body still selects/resizes it
        // (ReactFlow wires selection independently of the drag-handle
        // filter), it just doesn't start a move.
        dragHandle: n.type === 'groupNode' ? '.group-drag-handle' : undefined,
        // Belt-and-suspenders for a disabled group's children: the group's
        // own hatch overlay (now on top, see zIndex above) already blocks
        // clicks from reaching them, but explicitly marking them
        // non-interactive too covers any edge (e.g. a child peeking past a
        // rounded corner) and disables keyboard-based selection.
        selectable: parentDisabled ? false : undefined,
        draggable: parentDisabled ? false : undefined,
        data: {
          ...n.data,
          state: nodeStates[n.id] || n.data?.state || 'PENDING',
          extRunning: extRunSet.has(n.id) ||
            (n.type === 'groupNode' && Boolean(n.data?.folded) &&
             extNodes.some(c => c.parentNode === n.id && extRunSet.has(c.id))),
          connectedHandles: connectedByNode[n.id] || [],
          handleColors: handleColorByNode[n.id] || EMPTY_HANDLE_COLORS,
          handleHighlights: handleHiddenByNode[n.id] || EMPTY_HANDLE_SET,
          handleSelected: handleSelectedByNode[n.id] || EMPTY_HANDLE_SET,
          externalInputs,
          externalOutputs,
          underlying_groups: underlyingGroups,
          _showJumpOut: (n.type === 'ioNode' || n.type === 'groupNode') &&
            ((hoveredNodeId === n.id && !jumpDragFrom) || jumpDragFrom === n.id),
          _showJumpIn: (n.type === 'ioNode' || n.type === 'groupNode') &&
            Boolean(jumpDragFrom) && jumpDragFrom !== n.id && hoveredNodeId === n.id,
          onTitleChange: handleTitleChange,
          onToggleFold: handleToggleFold,
          onResize: handleGroupResize,
          onHandleClick: handleHandleClick,
          onUnderlyingGroupClick: handleUnderlyingGroupClick,
        },
      }
    })
    // A disabled group always ties at the SAME rendered z as its own
    // children (React Flow's parent->child clamp - see the comment on
    // zIndex above), so among tied elements, stacking falls back to array
    // order. Array.prototype.sort is stable (ES2019+), so this only moves
    // disabled groups to the end RELATIVE to everything else, preserving
    // every other ordering — guaranteeing a disabled group always sorts
    // after its own children (wherever a newly created one landed in the
    // array) without needing to touch anything else's position.
    mapped.sort((a, b) => {
      const rankA = a.type === 'groupNode' && disabledGroups.has(a.id) ? 1 : 0
      const rankB = b.type === 'groupNode' && disabledGroups.has(b.id) ? 1 : 0
      return rankA - rankB
    })

    setNodes(prev => {
      const selIds = new Set(prev.filter(n => n.selected).map(n => n.id))
      const prevById = new Map(prev.map(n => [n.id, n]))
      return mapped.map(n => {
        const cur = prevById.get(n.id)
        // Mid-drag, React Flow tracks the live position itself (node.dragging,
        // set by applyNodeChanges) — extNodes still holds the pre-drag position
        // until onNodeDragStop commits it. This effect reruns on every
        // nodeStates/externalRunning tick (status polls every ~2s, more often
        // during a run) independent of any drag, so without this guard it
        // would stomp the in-progress position back to the old spot on every
        // tick, making the node visibly jump while being dragged.
        if (cur?.dragging) {
          return { ...n, position: cur.position, dragging: cur.dragging, selected: selIds.has(n.id) }
        }
        // Same problem, for NodeResizer's own gesture: it marks `resizing:
        // true` on the internal node (mirroring `dragging`) and live-applies
        // width/height to `style` (and, for a corner-handle resize that
        // inverts x/y, `position` too) on every tick, only settling into
        // extNodes on onResizeEnd via handleGroupResize. Without this guard,
        // the same poll-triggered re-sync stomps the in-progress size back
        // to the last-committed (pre-resize) style, producing a visible
        // "jump" mid-resize followed by a second snap on mouse-up. Kept as
        // its own branch (not merged with the dragging one) since a plain
        // drag must NOT freeze `style` — a fold-toggle can legitimately
        // change it mid-drag and should still apply immediately.
        if (cur?.resizing) {
          return { ...n, position: cur.position, style: cur.style, resizing: cur.resizing, selected: selIds.has(n.id) }
        }
        return { ...n, selected: selIds.has(n.id) }
      })
    })
  }, [extNodes, extEdges, nodeStates, externalRunning, handleToggleFold, handleTitleChange, handleGroupResize, hoveredNodeId, jumpDragFrom, selectedEdges, handleHandleClick, handleUnderlyingGroupClick]) // eslint-disable-line

  // ── Effect 2: EDGE sync ───────────────────────────────────────────────────
  // Runs ONLY when edges or group structure changes — NOT on plain node moves.
  // Uses extNodesRef so we get fresh node data without adding extNodes to deps.
  useEffect(() => {
    const currentNodes = extNodesRef.current
    const foldedGroups = new Set(currentNodes.filter(n => n.type === 'groupNode' && n.data?.folded).map(n => n.id))
    const hiddenNodeIds = new Set(currentNodes.filter(n => n.parentNode && foldedGroups.has(n.parentNode)).map(n => n.id))

    const hiddenEdgeIds = new Set()
    const synEdges = []

    if (foldedGroups.size > 0) {
      extEdges.forEach(e => {
        // Redirect EACH hidden endpoint to its folded group — an edge can have
        // one or both ends hidden (e.g. a link between two folded groups).
        const srcGroup = hiddenNodeIds.has(e.source)
          ? currentNodes.find(n => n.id === e.source)?.parentNode : null
        const tgtGroup = hiddenNodeIds.has(e.target)
          ? currentNodes.find(n => n.id === e.target)?.parentNode : null
        if (!srcGroup && !tgtGroup) return
        hiddenEdgeIds.add(e.id)
        // v1 limitation: jump edges don't fold into the synthetic ext-*
        // handle system (that exists for group port routing chains, which
        // jump edges — plain execution-order annotations — don't have). The
        // edge just stays hidden while the endpoint is folded away and
        // reappears on unfold; the underlying jump_links entry is untouched.
        if (e.sourceHandle === 'jump-out' || e.targetHandle === 'jump-in') return
        if (srcGroup && srcGroup === tgtGroup) return // internal to one folded group
        // Port edge between a folded group and its own hidden child — the
        // routing is implied by the port itself, nothing to synthesize.
        if (tgtGroup === e.source || srcGroup === e.target) return
        synEdges.push({
          id: `syn-${e.id}`,
          source: srcGroup || e.source,
          sourceHandle: srcGroup ? `ext-out-${e.source}__${e.sourceHandle}` : e.sourceHandle,
          target: tgtGroup || e.target,
          targetHandle: tgtGroup ? `ext-in-${e.target}__${e.targetHandle}` : e.targetHandle,
          style: { stroke: '#63b3ed', strokeDasharray: '4 2' },
        })
      })
    }

    // Links on the inner side of a group port (port → child input, or child
    // output → port) are routing continuations, drawn dotted in port color.
    const groupIds = new Set(currentNodes.filter(n => n.type === 'groupNode').map(n => n.id))
    const isInnerPortEdge = e =>
      (groupIds.has(e.source) && (e.sourceHandle || '').startsWith('gin-')) ||
      (groupIds.has(e.target) && (e.targetHandle || '').startsWith('gout-'))
    const isJumpEdge = e => e.sourceHandle === 'jump-out' || e.targetHandle === 'jump-in'

    // Uses functional setEdges(prev=>) to preserve ReactFlow's selection flag
    // — this effect reruns on every handle_color/hide edit (both change
    // extEdges), and without carrying `.selected` forward by id, RF's own
    // selection bookkeeping (derived from the edges array it's given) would
    // see "no edge selected" on the very next render and immediately clear
    // the just-edited link's selection out from under the Properties panel.
    // A handle dot is shared by every edge attached to it — with a fan-out
    // (or fan-in), coloring one edge's handle necessarily recolors that same
    // shared dot for its siblings too, since ReactFlow renders one dot per
    // port, not one per edge (see Effect 1's handleColorByNode). The WIRE
    // itself has no such limit — each edge draws its own path — so an edge's
    // own `data.handle_color` always tints its own line here, keeping it
    // visually distinct from same-handle siblings whose color wasn't set,
    // even though the shared dot can only show one of them.
    setEdges(prev => {
      const selIds = new Set(prev.filter(e => e.selected).map(e => e.id))
      return [...extEdges.map(e => {
        const own = e.data?.handle_color
        const style = isJumpEdge(e)
          ? { ...e.style, stroke: own || '#9f7aea', strokeDasharray: '6 3' }
          : isInnerPortEdge(e)
          ? { ...e.style, stroke: own || '#d69e2e', strokeDasharray: '2 4', opacity: 0.75 }
          : own
          ? { ...e.style, stroke: own }
          : e.style
        return {
          ...e,
          selected: selIds.has(e.id),
          hidden: hiddenEdgeIds.has(e.id) || Boolean(e.data?.hide),
          style,
        }
      }), ...synEdges]
    })
  }, [extEdges, foldedKey, parentKey]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Connect ───────────────────────────────────────────────────────────────
  // Group ports are routers with fixed directions: a group input (gin-) takes
  // links from outside the group and feeds nodes inside it; a group output
  // (gout-) takes links from inside and feeds the outside. The synthetic
  // folded ext-* handles only visualize auto-detected connections and are
  // never connectable.
  const isValidGroupConnection = useCallback((params) => {
    const nds = extNodesRef.current
    const src = nds.find(n => n.id === params.source)
    const tgt = nds.find(n => n.id === params.target)
    if (!src || !tgt) return false
    const srcIsGroup = src.type === 'groupNode'
    const tgtIsGroup = tgt.type === 'groupNode'
    // Jump ports: execution-order-only, no data. A group acts as a jump
    // router exactly like it does for data ports — its own `jump-in`/
    // `jump-out` handle plays two roles sharing that id (see GroupNode.jsx):
    // an OUTER role (type matches a plain node's — target 'jump-in' /
    // source 'jump-out', for connections crossing the group boundary) and an
    // INNER role (the opposite type on the SAME id — source 'jump-in' feeds
    // a child inside; target 'jump-out' accepts one from a child), used for
    // the pass-through hop to/from a specific child. Only a group renders
    // the inner-role handles at all, so `sourceHandle === 'jump-in'` /
    // `targetHandle === 'jump-out'` unambiguously mean "this is a group's
    // inner role".
    const srcIsJump = params.sourceHandle === 'jump-out' || params.sourceHandle === 'jump-in'
    const tgtIsJump = params.targetHandle === 'jump-in' || params.targetHandle === 'jump-out'
    if (srcIsJump || tgtIsJump) {
      if (!(srcIsJump && tgtIsJump)) return false
      if (params.source === params.target) return false
      if (!srcIsGroup && !tgtIsGroup) {
        // Plain node-to-node: the only real shape a node offers.
        return params.sourceHandle === 'jump-out' && params.targetHandle === 'jump-in'
      }
      if (srcIsGroup) {
        if (params.sourceHandle === 'jump-in') {
          // Inner: group's jump-in continues to a child inside it.
          if (tgtIsGroup) return false
          return params.targetHandle === 'jump-in' && tgt.parentNode === params.source
        }
        // Outer: group's jump-out feeds outward — any non-child node, or
        // another group's jump-in (sibling/nested routing).
        if (tgtIsGroup) return params.targetHandle === 'jump-in' && params.target !== params.source
        return params.targetHandle === 'jump-in' && tgt.parentNode !== params.source
      }
      // src is a plain node, tgt is a group.
      if (params.targetHandle === 'jump-in') {
        // Outer: entering the group from outside (a child of THIS group must
        // use the inner 'jump-out' role instead, not this one).
        return params.sourceHandle === 'jump-out' && src.parentNode !== params.target
      }
      // targetHandle === 'jump-out': inner, a child feeding the group's exit.
      return params.sourceHandle === 'jump-out' && src.parentNode === params.target
    }
    if (!srcIsGroup && !tgtIsGroup) return true
    const sh = params.sourceHandle || ''
    const th = params.targetHandle || ''
    if (srcIsGroup && !(sh.startsWith('gin-') || sh.startsWith('gout-'))) return false
    if (tgtIsGroup && !(th.startsWith('gin-') || th.startsWith('gout-'))) return false
    if (srcIsGroup) {
      if (sh.startsWith('gin-')) {
        // Input port feeds inward: a child node, or the same group's output
        // port (pass-through).
        if (tgtIsGroup) return params.target === params.source && th.startsWith('gout-')
        return tgt.parentNode === params.source
      }
      // Output port feeds outward: any non-child, or another group's input.
      if (tgtIsGroup) return params.target !== params.source && th.startsWith('gin-')
      return tgt.parentNode !== params.source
    }
    // Plain node → group port
    if (th.startsWith('gin-')) return src.parentNode !== params.target
    return src.parentNode === params.target // gout- takes inside outputs
  }, [])

  const onConnect = useCallback(params => {
    if (!isValidGroupConnection(params)) return
    // A node's jump-out feeds exactly one target (unlike jump-in, which
    // accepts fan-in) — wiring a new jump-out connection replaces whichever
    // one it already had, rather than adding a second. A group's inner
    // jump-in→child pass-through is the same single-target contract (its
    // source handle happens to be 'jump-in', not 'jump-out' — see the
    // isValidGroupConnection comment above).
    let baseEdges = extEdgesRef.current
    if (params.sourceHandle === 'jump-out' ||
        (params.sourceHandle === 'jump-in' && extNodesRef.current.find(n => n.id === params.source)?.type === 'groupNode')) {
      baseEdges = baseEdges.filter(e => !(e.source === params.source && e.sourceHandle === params.sourceHandle))
    } else if (params.targetHandle !== 'jump-in' && !(params.targetHandle || '').startsWith('gin-') && !(params.targetHandle || '').startsWith('gout-')) {
      // A plain node input takes a single incoming value — the backend
      // already resolves it that way (last edge wins, see server.py's
      // edge_map dict), so a new connection here must replace the old one
      // instead of adding a second edge that only disappears on reload.
      baseEdges = baseEdges.filter(e => !(e.target === params.target && e.targetHandle === params.targetHandle))
    }
    onGraphChange(extNodesRef.current, addEdge({ ...params, animated: false }, baseEdges))
  }, [onGraphChange, isValidGroupConnection])

  // ── Jump-port hover/drag reveal ────────────────────────────────────────────
  const onNodeMouseEnter = useCallback((_, node) => setHoveredNodeId(node.id), [])
  const onNodeMouseLeave = useCallback(() => setHoveredNodeId(null), [])
  const onConnectStart = useCallback((_, { nodeId, handleId, handleType }) => {
    // A node's only source-role jump handle is 'jump-out'; a group's inner
    // pass-through additionally starts a jump drag from its 'jump-in' handle
    // used in the SOURCE role (see GroupNode.jsx's GroupJumpPorts) — the
    // handleType check is what disambiguates it from the group's own
    // target-role 'jump-in' (which merely receives a drag, doesn't start one).
    if (handleId === 'jump-out' || (handleId === 'jump-in' && handleType === 'source')) {
      setJumpDragFrom(nodeId)
    }
  }, [])
  const onConnectEnd = useCallback(() => setJumpDragFrom(null), [])

  // ── Drag-stop ─────────────────────────────────────────────────────────────
  // Commits every dragged node's new position at once — used both for a
  // node-initiated multi-drag (onNodeDragStop's 3rd arg) and for a drag
  // started from within the selection bounding box, which ReactFlow routes
  // through the *separate* onSelectionDragStop callback instead (its own
  // drag handler, with no equivalent to onNodeDragStop's single-node path).
  // Without wiring both, dragging via the selection box silently never
  // persisted — positions would revert on the next state resync (e.g. hover).
  const commitDraggedPositions = useCallback((draggedNodes) => {
    const extN = extNodesRef.current, extE = extEdgesRef.current
    const posById = new Map(draggedNodes.map(n => [n.id, n.position]))
    onGraphChange(extN.map(n => posById.has(n.id) ? { ...n, position: posById.get(n.id) } : n), extE)
  }, [onGraphChange])

  const onSelectionDragStop = useCallback((_, draggedNodes) => {
    commitDraggedPositions(draggedNodes)
  }, [commitDraggedPositions])

  const onNodeDragStop = useCallback((_, draggedNode, allDragged) => {
    const extN = extNodesRef.current, extE = extEdgesRef.current
    if (allDragged && allDragged.length > 1) {
      commitDraggedPositions(allDragged)
      return
    }
    if (draggedNode.type === 'groupNode') {
      onGraphChange(extN.map(n => n.id === draggedNode.id ? { ...n, position: draggedNode.position } : n), extE)
      return
    }
    const absPos = draggedNode.positionAbsolute || draggedNode.position
    // Folded groups don't accept drops: their stored style still has the
    // expanded size, and adding into an invisible area would be confusing.
    const targetGroup = extN.find(n =>
      n.type === 'groupNode' && !n.data?.folded && pointInGroup(absPos.x, absPos.y, n))
    let updated
    if (targetGroup) {
      updated = extN.map(n =>
        n.id === draggedNode.id
          ? { ...n, parentNode: targetGroup.id, extent: 'parent', position: { x: Math.max(4, absPos.x - targetGroup.position.x), y: Math.max(36, absPos.y - targetGroup.position.y) } }
          : n
      )
    } else if (draggedNode.parentNode) {
      const parent = extN.find(n => n.id === draggedNode.parentNode)
      updated = extN.map(n =>
        n.id === draggedNode.id
          ? { ...n, parentNode: undefined, extent: undefined, position: { x: draggedNode.position.x + (parent?.position.x || 0), y: draggedNode.position.y + (parent?.position.y || 0) } }
          : n
      )
    } else {
      updated = extN.map(n => n.id === draggedNode.id ? { ...n, position: draggedNode.position } : n)
    }
    onGraphChange(updated, extE)
  }, [onGraphChange])

  // ── Selection ─────────────────────────────────────────────────────────────
  // Only the synthetic folded-group edges (syn-*) have no Properties-panel
  // entry. Every real link does — plain data links, jump links, and group
  // port routing links (outside -> gin, gin -> child, child -> gout, gout ->
  // outside, group -> group) — so each can get a color and be hidden.
  const isSelectableEdge = useCallback(e => !e.id.startsWith('syn-'), [])

  // A rubber-band/shift-click multi-selection only becomes a genuine "group"
  // (Properties panel color-shared view) if every edge in it shares a handle
  // — otherwise there's no single handle whose color it would actually be
  // editing, so it collapses to nothing selected, same as before. This ALSO
  // runs for handleHandleClick's own setEdges call above (ReactFlow fires
  // onSelectionChange reactively whenever the edges' `.selected` flags
  // change, regardless of what changed them) — its selection always shares a
  // handle by construction, so this mirrors it back unchanged rather than
  // clobbering it back to empty.
  const edgesShareAHandle = sel => {
    if (sel.length < 2) return true
    const [first, ...rest] = sel
    const shareSource = rest.every(e => e.source === first.source && e.sourceHandle === first.sourceHandle)
    const shareTarget = rest.every(e => e.target === first.target && e.targetHandle === first.targetHandle)
    return shareSource || shareTarget
  }

  const onSelectionChange = useCallback(({ nodes: sel, edges: selE }) => {
    onSelectNode(sel.length === 1 ? sel[0] : null)
    const plainSel = selE.filter(isSelectableEdge)
    onSelectEdges(edgesShareAHandle(plainSel) ? plainSel : [])
  }, [onSelectNode, onSelectEdges, isSelectableEdge])

  // ── Delete nodes ──────────────────────────────────────────────────────────
  // A group's children whose `parentNode` recursively resolves back to `groupId`
  // (nested groups included) — used to cascade-delete a group instead of either
  // of the two things this used to do: silently orphan children with a
  // dangling `parentNode` (the old right-click path, App.jsx's onDeleteNode —
  // a known cause of a hard crash on the next graph load) or merely ungroup
  // them (the old Delete-key path, right here).
  const collectDescendants = (groupId, allNodes) => {
    const kids = allNodes.filter(n => n.parentNode === groupId)
    return kids.flatMap(k => [k.id, ...(k.type === 'groupNode' ? collectDescendants(k.id, allNodes) : [])])
  }

  // Deletes `deleted` (an array of RF node objects — the directly targeted
  // ones, e.g. one from a right-click or several from a selection), expanding
  // any group among them to include everything inside it. Confirms once, only
  // when a group is actually involved — a plain node (or several) still
  // deletes immediately, same as before.
  const onNodesDelete = useCallback(deleted => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const idSet = new Set(deleted.map(n => n.id))
    const groups = deleted.filter(n => n.type === 'groupNode')
    let descendantCount = 0
    groups.forEach(g => collectDescendants(g.id, nds).forEach(id => {
      if (!idSet.has(id)) descendantCount++
      idSet.add(id)
    }))
    if (groups.length > 0) {
      const label = groups.length === 1
        ? `group "${groups[0].data?.label || groups[0].id}"`
        : `${groups.length} groups`
      const msg = descendantCount > 0
        ? `Delete ${label} and the ${descendantCount} node(s) inside it? This cannot be undone.`
        : `Delete ${label}? This cannot be undone.`
      if (!window.confirm(msg)) return
    }
    onGraphChange(
      nds.filter(n => !idSet.has(n.id)),
      eds.filter(e => !idSet.has(e.source) && !idSet.has(e.target))
    )
    idSet.forEach(nid => onDeleteNode?.(nid))
  }, [onGraphChange, onDeleteNode])

  // ── Delete edges: clear reference input_values on target node ─────────────
  const onEdgesDelete = useCallback(deleted => {
    const real = deleted.filter(e => !e.id.startsWith('syn-'))
    if (real.length === 0) return
    const ids = new Set(real.map(e => e.id))
    let nds = extNodesRef.current
    real.forEach(e => {
      if (!e.targetHandle) return
      nds = nds.map(n => {
        if (n.id !== e.target) return n
        const vals = { ...(n.data?.input_values || {}) }
        if (typeof vals[e.targetHandle] === 'string' && REF_RE.test(vals[e.targetHandle])) {
          vals[e.targetHandle] = null
        }
        return { ...n, data: { ...n.data, input_values: vals } }
      })
    })
    onGraphChange(nds, extEdgesRef.current.filter(e => !ids.has(e.id)))
    if ((selectedEdges || []).some(se => ids.has(se.id))) {
      onSelectEdges((selectedEdges || []).filter(se => !ids.has(se.id)))
    }
  }, [onGraphChange, selectedEdges, onSelectEdges])

  // ── Delete key ────────────────────────────────────────────────────────────
  // `deleteKeyCode` is disabled above (`null`) — ReactFlow's own handling
  // applies the removal to local state BEFORE calling onNodesDelete, which
  // would make a group's confirm-to-cancel flicker the group back in after
  // ReactFlow had already optimistically removed it. Doing it ourselves means
  // nothing touches state until the (synchronous, blocking) confirm resolves.
  useEffect(() => {
    const onKeyDown = e => {
      if (e.key !== 'Delete') return
      const tag = e.target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target?.isContentEditable) return
      const selNodes = localNodesRef.current.filter(n => n.selected)
      const selEdges = localEdgesRef.current.filter(ed => ed.selected)
      if (selNodes.length === 0 && selEdges.length === 0) return
      e.preventDefault()
      if (selNodes.length > 0) onNodesDelete(selNodes)
      // Edges already covered by a deleted node's endpoint are removed as
      // part of that cascade above — only pass along ones that were
      // independently selected (a pure wire delete).
      const nodeIds = new Set(selNodes.map(n => n.id))
      const looseEdges = selEdges.filter(ed => !nodeIds.has(ed.source) && !nodeIds.has(ed.target))
      if (looseEdges.length > 0) onEdgesDelete(looseEdges)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onNodesDelete, onEdgesDelete])

  // ── Context menus ─────────────────────────────────────────────────────────
  const onPaneContextMenu = useCallback(e => {
    e.preventDefault()
    const bounds = reactFlowWrapper.current?.getBoundingClientRect()
    setCtxMenu({ x: e.clientX, y: e.clientY, nodeId: null,
      flowPos: project({ x: e.clientX - (bounds?.left || 0), y: e.clientY - (bounds?.top || 0) }) })
  }, [project])

  const onNodeContextMenu = useCallback((e, node) => {
    e.preventDefault(); e.stopPropagation()
    setCtxMenu({
      x: e.clientX, y: e.clientY, nodeId: node.id, hasParent: !!node.parentNode,
      // Pipeline input/output marking applies to executable nodes only.
      ioFlags: node.type === 'ioNode'
        ? { isInput: !!node.data?.is_input, isOutput: !!node.data?.is_output }
        : null,
      // Cache freezing applies to executable nodes only (groups have no cache).
      frozen: node.type === 'ioNode' ? !!node.data?.frozen : null,
    })
  }, [])

  // ── Sidebar drag-and-drop ─────────────────────────────────────────────────
  const handleDrop = useCallback(e => {
    e.preventDefault()
    const type = e.dataTransfer.getData('application/kpipeliner-node-type')
    if (!type) return
    const bounds = reactFlowWrapper.current?.getBoundingClientRect()
    onAddNodeAtPosition(type, project({ x: e.clientX - (bounds?.left || 0), y: e.clientY - (bounds?.top || 0) }))
  }, [project, onAddNodeAtPosition])

  const handleDragOver = useCallback(e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }, [])

  // ── Remove from group ─────────────────────────────────────────────────────
  const removeFromGroup = useCallback(nodeId => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const node = nds.find(n => n.id === nodeId)
    if (!node?.parentNode) return
    const parent = nds.find(n => n.id === node.parentNode)
    onGraphChange(nds.map(n =>
      n.id === nodeId
        ? { ...n, parentNode: undefined, extent: undefined, position: { x: node.position.x + (parent?.position.x || 0), y: node.position.y + (parent?.position.y || 0) } }
        : n
    ), eds)
  }, [onGraphChange])

  // ── Duplicate ─────────────────────────────────────────────────────────────
  const duplicateNode = useCallback(nodeId => {
    const nds = extNodesRef.current, eds = extEdgesRef.current
    const orig = nds.find(n => n.id === nodeId)
    if (!orig) return
    const newId = `${orig.data?.node_type || orig.id}_copy`
    onGraphChange([...nds, {
      ...orig, id: newId, parentNode: undefined, extent: undefined,
      position: { x: (orig.positionAbsolute?.x || orig.position.x) + 40, y: (orig.positionAbsolute?.y || orig.position.y) + 40 },
      data: { ...orig.data, label: newId },
    }], eds)
  }, [onGraphChange])

  const bg = guiConfig?.background || {}
  const bgPattern = bg.pattern || 'dots'
  const gridGap = Number(bg.gap) > 0 ? Number(bg.gap) : 20
  const snapEnabled = guiConfig?.snap?.enabled !== false

  return (
    <div ref={reactFlowWrapper} style={{ width: '100%', height: '100%' }} onDrop={handleDrop} onDragOver={handleDragOver}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
        onConnect={onConnect} onNodeDragStop={onNodeDragStop} onSelectionDragStop={onSelectionDragStop}
        onConnectStart={onConnectStart} onConnectEnd={onConnectEnd}
        onNodeMouseEnter={onNodeMouseEnter} onNodeMouseLeave={onNodeMouseLeave}
        onSelectionChange={onSelectionChange} onNodeClick={onNodeClick} onNodeDoubleClick={onNodeDoubleClick}
        onPaneContextMenu={onPaneContextMenu} onNodeContextMenu={onNodeContextMenu}
        onPaneClick={onPaneClickWithGroupFocus}
        deleteKeyCode={null} fitView
        minZoom={0.1} maxZoom={2}
        snapToGrid={snapEnabled} snapGrid={[gridGap, gridGap]}
        proOptions={{ hideAttribution: true }}
        style={{ background: '#0f1823' }}
      >
        {bgPattern !== 'none' && (
          <Background
            variant={bgPattern}
            color={bg.color || '#2d3748'}
            gap={gridGap}
            size={Number(bg.size) > 0 ? Number(bg.size) : 1}
          />
        )}
        <Controls style={{ background: '#2d3748', border: '1px solid #4a5568' }} />
        <MiniMap
          style={{ background: '#1a202c', border: '1px solid #4a5568' }}
          nodeColor={n =>
            nodeStates[n.id] === 'COMPLETED' ? '#2b6cb0' :
            nodeStates[n.id] === 'RUNNING'   ? '#276749' :
            nodeStates[n.id] === 'FAILED'    ? '#c53030' :
            n.type === 'groupNode'            ? '#2d4a6b' : '#2d3748'
          }
        />
      </ReactFlow>

      {ctxMenu && (
        <ContextMenu
          x={ctxMenu.x} y={ctxMenu.y} nodeId={ctxMenu.nodeId} ioFlags={ctxMenu.ioFlags} frozen={ctxMenu.frozen ?? null} nodeTypes={ntypes}
          onToggleIOFlag={onToggleIOFlag}
          onClose={() => setCtxMenu(null)}
          onAddNode={type => onAddNodeAtPosition(type, ctxMenu.flowPos || { x: 200, y: 200 })}
          onDeleteNode={nid => {
            const n = extNodesRef.current.find(x => x.id === nid)
            if (n) onNodesDelete([n])
            setCtxMenu(null)
          }}
          onRunNode={onRunNode}
          onAddGroup={() => { onAddGroupNode(ctxMenu.flowPos || { x: 200, y: 200 }); setCtxMenu(null) }}
          onDuplicateNode={duplicateNode}
          onRemoveFromGroup={ctxMenu.hasParent ? removeFromGroup : null}
        />
      )}
    </div>
  )
}

export default function GraphPanel(props) {
  return (
    <ReactFlowProvider>
      <GraphInner {...props} />
    </ReactFlowProvider>
  )
}
