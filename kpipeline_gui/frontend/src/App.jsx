import { useState, useEffect, useRef, useCallback } from 'react'
import axios from 'axios'
import Toolbar from './components/Toolbar.jsx'
import GraphPanel from './components/GraphPanel.jsx'
import ComponentPanel from './components/ComponentPanel.jsx'
import PropertiesPanel from './components/PropertiesPanel.jsx'
import TerminalPanel from './components/TerminalPanel.jsx'

const API = '/api'
const WS_URL = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

const DEFAULT_GUI_CONFIG = {
  background: { pattern: 'dots', gap: 20, size: 1, color: '#2d3748' },
  snap: { enabled: true },
}

// Components (left) / Properties (right) / Terminal (bottom) panel layout —
// purely a per-browser UI preference, so it lives in localStorage rather
// than round-tripping through config.yaml like the shared gui config above.
const PANEL_PREFS_KEY = 'kpl_panel_prefs'
const DEFAULT_PANEL_SIZES = { left: 220, right: 280, bottom: 260 }
const DEFAULT_PANEL_VISIBLE = { left: true, right: true, bottom: true }
const PANEL_MIN = { left: 160, right: 200, bottom: 120 }
const PANEL_MAX = { left: 560, right: 640, bottom: 640 }

function loadPanelPrefs() {
  try {
    const parsed = JSON.parse(localStorage.getItem(PANEL_PREFS_KEY))
    return {
      sizes: { ...DEFAULT_PANEL_SIZES, ...(parsed?.sizes || {}) },
      visible: { ...DEFAULT_PANEL_VISIBLE, ...(parsed?.visible || {}) },
    }
  } catch {
    return { sizes: DEFAULT_PANEL_SIZES, visible: DEFAULT_PANEL_VISIBLE }
  }
}

// Drag-to-resize strip. `orientation` picks the axis (vertical: drag
// horizontally to resize a sidebar's width; horizontal: drag vertically to
// resize the terminal's height) — onResize gets the raw pointer delta along
// that axis, sign/clamping is the caller's job (depends which edge of the
// panel the handle sits on).
function ResizeHandle({ orientation, onResize, style, className = '' }) {
  const draggingRef = useRef(false)
  const lastPosRef = useRef(0)

  useEffect(() => {
    function onMove(e) {
      if (!draggingRef.current) return
      const pos = orientation === 'vertical' ? e.clientX : e.clientY
      onResize(pos - lastPosRef.current)
      lastPosRef.current = pos
    }
    function onUp() {
      if (!draggingRef.current) return
      draggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [orientation, onResize])

  const onMouseDown = useCallback(e => {
    e.preventDefault()
    draggingRef.current = true
    lastPosRef.current = orientation === 'vertical' ? e.clientX : e.clientY
    document.body.style.cursor = orientation === 'vertical' ? 'col-resize' : 'row-resize'
    document.body.style.userSelect = 'none'
  }, [orientation])

  return (
    <div
      className={`resize-handle resize-handle-${orientation} ${className}`}
      style={style}
      onMouseDown={onMouseDown}
    />
  )
}

export default function App() {
  const [nodeTypes, setNodeTypes] = useState([])
  const [guiConfig, setGuiConfig] = useState(DEFAULT_GUI_CONFIG)
  const [nodes, setNodes] = useState([])
  const [edges, setEdges] = useState([])
  const [selectedNode, setSelectedNode] = useState(null)
  // Usually 0 or 1 edge; a handle-click on a fan-out/fan-in handle (see
  // GraphPanel's handleHandleClick) can select several at once, so the
  // Properties panel can show/edit them as a group.
  const [selectedEdges, setSelectedEdges] = useState([])
  const [nodeStates, setNodeStates] = useState({})
  const [logs, setLogs] = useState([])
  const [progress, setProgress] = useState({})
  const [isRunning, setIsRunning] = useState(false)
  // Pending single-node run awaiting the clear-cache confirmation: { nodeId, debug }
  const [confirmRun, setConfirmRun] = useState(null)
  // Graph held back because some nodes are missing from the base nodes:
  // { missing: [{name, type}], nodes, edges }
  const [missingPrompt, setMissingPrompt] = useState(null)
  // A node type is being created whose next unresolved underlying_groups
  // slot declares an `address` that no group component exists at.
  // { nt, pos, queue, resolved, skipped, slot, category, name } — queue/
  // resolved/skipped carry the in-progress slot-resolution state so
  // "Ignore & Continue" can resume where it left off.
  const [groupAddressPrompt, setGroupAddressPrompt] = useState(null)
  const [systemStats, setSystemStats] = useState({ cpu: 0, gpu: null, mem: 0 })
  // Nodes currently running in a terminal `kpl run` in the same directory —
  // purely a highlight; does NOT set isRunning, the GUI stays fully usable.
  const [externalRunning, setExternalRunning] = useState([])
  const [wsConnected, setWsConnected] = useState(false)
  const wsRef = useRef(null)
  const saveTimerRef = useRef(null)

  // -------------------------------------------------------------------------
  // Panel layout (resize / hide) — Components, Properties, Terminal
  // -------------------------------------------------------------------------
  const [panelSizes, setPanelSizes] = useState(() => loadPanelPrefs().sizes)
  const [panelVisible, setPanelVisible] = useState(() => loadPanelPrefs().visible)

  useEffect(() => {
    localStorage.setItem(PANEL_PREFS_KEY, JSON.stringify({ sizes: panelSizes, visible: panelVisible }))
  }, [panelSizes, panelVisible])

  const togglePanel = useCallback((key) => {
    setPanelVisible(prev => ({ ...prev, [key]: !prev[key] }))
  }, [])

  // `sign` accounts for which edge of the panel the drag handle sits on:
  // the left sidebar grows when dragged right (+delta), the right sidebar
  // and the terminal grow when dragged left/up (-delta).
  const resizePanel = useCallback((key, delta) => {
    const sign = key === 'left' ? 1 : -1
    setPanelSizes(prev => ({
      ...prev,
      [key]: Math.min(PANEL_MAX[key], Math.max(PANEL_MIN[key], Math.round(prev[key] + sign * delta))),
    }))
  }, [])
  const resizeLeft = useCallback(d => resizePanel('left', d), [resizePanel])
  const resizeRight = useCallback(d => resizePanel('right', d), [resizePanel])
  const resizeBottom = useCallback(d => resizePanel('bottom', d), [resizePanel])

  // The server sends the (sorted) list every 2s — keep the previous array
  // identity when nothing changed, so downstream effects don't re-run.
  const updateExternalRunning = useCallback((list) => {
    const next = list || []
    setExternalRunning(prev =>
      prev.length === next.length && prev.every((v, i) => v === next[i]) ? prev : next)
  }, [])
  const pendingRef = useRef({ logs: [], progress: {}, states: {} })
  const rafRef = useRef(null)

  // -------------------------------------------------------------------------
  // WebSocket
  // -------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false
    let retryMs = 300
    function connect() {
      if (cancelled) return
      const ws = new WebSocket(WS_URL)
      wsRef.current = ws

      // Abort attempts that hang in CONNECTING (proxy, half-open server, …)
      // so the retry loop stays fast instead of waiting on a dead attempt.
      const hangGuard = setTimeout(() => {
        if (ws.readyState === WebSocket.CONNECTING) ws.close()
      }, 3000)

      ws.onopen = () => {
        clearTimeout(hangGuard)
        retryMs = 300
        setWsConnected(true)
      }
      ws.onclose = () => {
        clearTimeout(hangGuard)
        setWsConnected(false)
        if (!cancelled) setTimeout(connect, retryMs)
        retryMs = Math.min(retryMs * 2, 3000)
      }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        handleWsMessage(msg)
      }
    }
    connect()
    return () => {
      cancelled = true
      wsRef.current?.close()
    }
  }, [])

  // HTTP fallback while the WebSocket is down: poll /api/status so run state,
  // node states and CPU/GPU stats work immediately — a run started before the
  // WS is up stays visible instead of looking like "nothing happened".
  useEffect(() => {
    if (wsConnected) return
    function poll() {
      axios.get(`${API}/status`).then(r => {
        setIsRunning(!!r.data.running)
        setNodeStates(prev => ({ ...prev, ...(r.data.node_states || {}) }))
        setSystemStats({ cpu: r.data.cpu || 0, gpu: r.data.gpu, mem: r.data.mem || 0 })
        updateExternalRunning(r.data.external_running)
      }).catch(() => {})
    }
    poll()
    const iv = setInterval(poll, 2000)
    return () => clearInterval(iv)
  }, [wsConnected])

  // Batch high-frequency log/progress/state updates into one flush per animation
  // frame instead of one React state update per websocket message — a fast run
  // can emit hundreds of messages/sec, which otherwise floods rendering enough
  // to make the graph canvas stall or blank out until the page is reloaded.
  function scheduleFlush() {
    if (rafRef.current) return
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null
      const pending = pendingRef.current
      pendingRef.current = { logs: [], progress: {}, states: {} }
      if (pending.logs.length) {
        setLogs(prev => [...prev, ...pending.logs].slice(-2000))
      }
      if (Object.keys(pending.progress).length) {
        setProgress(prev => ({ ...prev, ...pending.progress }))
      }
      if (Object.keys(pending.states).length) {
        setNodeStates(prev => ({ ...prev, ...pending.states }))
      }
    })
  }

  function handleWsMessage(msg) {
    if (msg.type === 'log') {
      const line = msg.data || ''
      let cls = 'log-info'
      if (line.includes('[error]')) cls = 'log-error'
      else if (line.includes('[output]')) cls = 'log-output'
      else if (line.includes('[warning]')) cls = 'log-warn'
      pendingRef.current.logs.push({ text: line, cls })
      scheduleFlush()
    } else if (msg.type === 'progress') {
      const { message, progress: pct, max, status } = msg
      if (message !== undefined) {
        pendingRef.current.progress[msg.node || 'node'] = { progress: pct, max, message, status }
        scheduleFlush()
      }
    } else if (msg.type === 'state') {
      pendingRef.current.states[msg.node] = msg.state
      scheduleFlush()
    } else if (msg.type === 'run_start') {
      pendingRef.current = { logs: [], progress: {}, states: {} }
      setIsRunning(true)
      setLogs([])
      setProgress({})
      setNodeStates({})
    } else if (msg.type === 'run_end') {
      setIsRunning(false)
      const line = msg.success ? '✓ Run completed.' : '✗ Run failed.'
      pendingRef.current.logs.push({ text: line, cls: msg.success ? 'log-output' : 'log-error' })
      scheduleFlush()
    } else if (msg.type === 'system') {
      setSystemStats({ cpu: msg.cpu || 0, gpu: msg.gpu, mem: msg.mem || 0 })
      updateExternalRunning(msg.external_running)
      if (msg.node_states) {
        setNodeStates(prev => {
          const keys = Object.keys(msg.node_states)
          const changed = keys.length !== Object.keys(prev).length || keys.some(k => prev[k] !== msg.node_states[k])
          return changed ? { ...prev, ...msg.node_states } : prev
        })
      }
    } else if (msg.type === 'error') {
      pendingRef.current.logs.push({ text: msg.data, cls: 'log-error' })
      scheduleFlush()
    }
  }

  // -------------------------------------------------------------------------
  // Load initial data
  // -------------------------------------------------------------------------
  useEffect(() => {
    axios.get(`${API}/nodes`).then(r => setNodeTypes(r.data)).catch(() => {})
    axios.get(`${API}/config`).then(r => setGuiConfig(prev => ({ ...prev, ...r.data }))).catch(() => {})
    loadGraph()
    // Pick up an already-running pipeline (e.g. after a page refresh) so the
    // Stop button isn't stuck disabled until the run happens to finish.
    axios.get(`${API}/status`).then(r => {
      setIsRunning(!!r.data.running)
      setNodeStates(r.data.node_states || {})
      updateExternalRunning(r.data.external_running)
    }).catch(() => {})
  }, [])

  function loadGraph() {
    axios.get(`${API}/graph`).then(r => {
      const nds = r.data.nodes || []
      const eds = r.data.edges || []
      const missing = r.data.missing || []
      if (missing.length > 0) {
        // Ask before loading: the graph references node types that are not
        // installed in the base nodes. Ignore & load, or cancel loading.
        setMissingPrompt({ missing, nodes: nds, edges: eds })
      } else {
        setNodes(nds)
        setEdges(eds)
      }
    }).catch(() => {})
  }

  // -------------------------------------------------------------------------
  // Save graph (debounced)
  // -------------------------------------------------------------------------
  const saveGraph = useCallback((nds, eds) => {
    clearTimeout(saveTimerRef.current)
    saveTimerRef.current = setTimeout(() => {
      axios.post(`${API}/graph`, { nodes: nds, edges: eds }).catch(() => {})
    }, 500)
  }, [])

  // -------------------------------------------------------------------------
  // Undo / redo (Ctrl+Z / Ctrl+Shift+Z)
  // -------------------------------------------------------------------------
  // Every graph mutation flows through handleGraphChange, so history is a
  // stack of {nodes, edges} snapshots taken there. Changes arriving in quick
  // succession (typing a value in the property bar) coalesce into one entry.
  const HISTORY_LIMIT = 100
  const HISTORY_COALESCE_MS = 500

  const nodesRef = useRef(nodes)
  const edgesRef = useRef(edges)
  nodesRef.current = nodes
  edgesRef.current = edges
  const historyRef = useRef({ past: [], future: [], lastPush: 0 })

  // Set + persist without touching history (used by undo/redo themselves).
  const applyGraph = useCallback((nds, eds) => {
    setNodes(nds)
    setEdges(eds)
    saveGraph(nds, eds)
  }, [saveGraph])

  // Memoized so it keeps a stable identity across the frequent re-renders
  // caused by log/progress updates during a run — GraphPanel's node-sync
  // effect depends on this (transitively), and an identity change on every
  // render was forcing that effect to re-run on nearly every websocket
  // message, adding to the rendering load during a run.
  const handleGraphChange = useCallback((nds, eds) => {
    const h = historyRef.current
    const now = Date.now()
    if (now - h.lastPush > HISTORY_COALESCE_MS) {
      h.past.push({ nodes: nodesRef.current, edges: edgesRef.current })
      if (h.past.length > HISTORY_LIMIT) h.past.shift()
    }
    h.lastPush = now
    h.future = []
    applyGraph(nds, eds)
  }, [applyGraph])

  // After restoring, re-point the selection at the restored node object (or
  // drop it if the node doesn't exist in that snapshot).
  const restoreSnapshot = useCallback((snap) => {
    applyGraph(snap.nodes, snap.edges)
    setSelectedNode(sel => (sel ? snap.nodes.find(n => n.id === sel.id) || null : null))
    setSelectedEdges(sels => sels.map(sel => snap.edges.find(e => e.id === sel.id)).filter(Boolean))
  }, [applyGraph])

  const undo = useCallback(() => {
    const h = historyRef.current
    if (h.past.length === 0) return
    const snap = h.past.pop()
    h.future.push({ nodes: nodesRef.current, edges: edgesRef.current })
    h.lastPush = 0 // the next change starts a fresh history entry
    restoreSnapshot(snap)
  }, [restoreSnapshot])

  const redo = useCallback(() => {
    const h = historyRef.current
    if (h.future.length === 0) return
    const snap = h.future.pop()
    h.past.push({ nodes: nodesRef.current, edges: edgesRef.current })
    h.lastPush = 0
    restoreSnapshot(snap)
  }, [restoreSnapshot])

  useEffect(() => {
    function onKey(e) {
      if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'z') return
      // Text fields keep the browser's own text undo.
      const t = e.target
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      e.preventDefault()
      if (e.shiftKey) redo()
      else undo()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo])

  // -------------------------------------------------------------------------
  // Run controls
  // -------------------------------------------------------------------------
  // Clears both the live run states and the states persisted in the graph —
  // GraphPanel falls back to data.state when nodeStates is empty, so leaving
  // it behind keeps stale colors on the canvas.
  function clearNodeColors() {
    setNodeStates({})
    if (nodes.some(n => n.data?.state && n.data.state !== 'PENDING')) {
      const cleared = nodes.map(n =>
        n.data?.state && n.data.state !== 'PENDING'
          ? { ...n, data: { ...n.data, state: 'PENDING' } }
          : n
      )
      handleGraphChange(cleared, edges)
    }
  }

  function executeRun(nodeId, debug, clearCache) {
    setConfirmRun(null)
    clearNodeColors()
    axios.post(`${API}/run`, { node_id: nodeId, debug, clear_cache: clearCache }).catch(() => {})
  }

  // Single-node run: only ask the clear-cache question when the node actually
  // has cached data; otherwise there is nothing to clear — run directly.
  function requestNodeRun(nodeId, debug) {
    axios.get(`${API}/node-cache/${encodeURIComponent(nodeId)}`)
      .then(r => {
        if (r.data?.cached) setConfirmRun({ nodeId, debug })
        else executeRun(nodeId, debug, false)
      })
      .catch(() => setConfirmRun({ nodeId, debug }))
  }

  // Run a group like self.groups['x'].run() in node code (`kpl group run`).
  function executeGroupRun(groupId) {
    clearNodeColors()
    // Dedicated endpoint: an older server answers 404 instead of silently
    // starting a full pipeline run.
    axios.post(`${API}/run-group`, { group_id: groupId }).catch(() => {
      setLogs(prev => [...prev, {
        text: 'Run Group failed — the GUI server is outdated; restart it (kpl gui).',
        cls: 'log-error',
      }])
    })
  }

  function handleRun(debug = false) {
    if (selectedNode) {
      requestNodeRun(selectedNode.id, debug)
    } else {
      executeRun(null, debug, false)
    }
  }

  function handleStop() {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'stop' }))
    }
    axios.post(`${API}/stop`).catch(() => {})
  }

  function handleReset() {
    clearNodeColors()
    axios.post(`${API}/reset`).catch(() => {})
  }

  function handleClearCache() {
    axios.post(`${API}/clear-cache`)
      .then(r => {
        const msg = r.data?.msg || 'Cache cleared.'
        setLogs(prev => [...prev, { text: msg, cls: 'log-output' }])
      })
      .catch(() => setLogs(prev => [...prev, { text: 'Clear cache failed.', cls: 'log-error' }]))
  }

  // -------------------------------------------------------------------------
  // Node property updates
  // -------------------------------------------------------------------------
  function handleNodeUpdate(nodeId, changes) {
    // A label change on a pipeline node is a rename: the label IS the node's
    // name in graph_config.yaml, so it must go through renameNode (which also
    // updates the id and edge references) or it would be lost on reload.
    if ('label' in changes) {
      const target = nodes.find(n => n.id === nodeId)
      if (target && target.type !== 'groupNode') {
        renameNode(nodeId, changes.label)
        return
      }
    }
    const updated = nodes.map(n => {
      if (n.id !== nodeId) return n
      return { ...n, data: { ...n.data, ...changes } }
    })
    handleGraphChange(updated, edges)
    if (selectedNode?.id === nodeId) {
      setSelectedNode(prev => ({ ...prev, data: { ...prev.data, ...changes } }))
    }
  }

  // Binding an underlying_groups slot (Properties panel dropdown) can touch
  // up to THREE nodes at once, unlike handleNodeUpdate above (always just
  // the one): the calling node's slot itself, the newly-bound target group,
  // and the group being unbound FROM (if any).
  function handleBindUnderlyingGroup(nodeId, slotName, newGroupId) {
    const callingNode = nodes.find(n => n.id === nodeId)
    const oldGroupId = callingNode?.data?.underlying_groups?.find(s => s.name === slotName)?.group_id || null

    let updated = nodes.map(n => {
      if (n.id === nodeId) {
        const ug = (n.data.underlying_groups || []).map(s =>
          s.name === slotName ? { ...s, group_id: newGroupId } : s
        )
        return { ...n, data: { ...n.data, underlying_groups: ug } }
      }
      return n
    })

    // Detaching (or rebinding to a different group): if NOTHING else
    // references the group we just left — no other slot on this or any
    // other node still points at it — it goes back to being an ordinary,
    // unmanaged group: disabled/highlight_color reset to null (not false),
    // the same "never explicitly set" state a group that was never bound to
    // anything starts in, so it renders/behaves exactly like any other
    // normal group again (not force-enabled, just no longer force-disabled
    // by a binding that no longer exists). Left alone if something ELSE
    // still references it — that reference still legitimately governs it.
    if (oldGroupId && oldGroupId !== newGroupId) {
      const stillReferenced = updated.some(n =>
        (n.data?.underlying_groups || []).some(s => s.group_id === oldGroupId)
      )
      if (!stillReferenced) {
        updated = updated.map(n =>
          n.id === oldGroupId ? { ...n, data: { ...n.data, disabled: null, highlight_color: null } } : n
        )
      }
    }

    // A fresh binding starts deselected, same as any other unselected slot
    // in the under-node chip list — not whatever disabled/highlight_color
    // that group happened to have from page-load time before this binding
    // existed.
    if (newGroupId) {
      updated = updated.map(n =>
        n.id === newGroupId ? { ...n, data: { ...n.data, disabled: true, highlight_color: null } } : n
      )
    }

    handleGraphChange(updated, edges)
    // Without this, the Properties panel keeps showing the PRE-change
    // binding until the node is deselected and reselected — `node={
    // selectedNode}` (App.jsx's own separate state, not derived from
    // `nodes`) never picks up `updated` above on its own. Same fix
    // handleNodeUpdate already applies for every other Properties-panel
    // field; this one just missed it originally.
    if (selectedNode?.id === nodeId) {
      const patchedNode = updated.find(n => n.id === nodeId)
      if (patchedNode) setSelectedNode(patchedNode)
    }
  }

  // Link (edge) property updates — handle_color / hide, set from the
  // Properties panel when a plain node-to-node edge (or a jump link) is
  // selected. `handle_color` belongs to the HANDLE, not just this one wire —
  // an output (or jump-out/jump-in) handle can carry several edges at once
  // (fan-out/fan-in), and they must all show the same color, same as the dot
  // itself is one shared visual element regardless of how many edges touch
  // it. `hide` stays per-edge on purpose — each wire can be individually
  // shown/hidden regardless of what color its siblings share.
  function handleEdgeUpdate(edgeId, changes) {
    const primary = edges.find(e => e.id === edgeId)
    if (!primary) return
    const siblingIds = new Set()
    if ('handle_color' in changes) {
      edges.forEach(e => {
        if (e.id === edgeId) return
        const sharesSource = e.source === primary.source && e.sourceHandle === primary.sourceHandle
        const sharesTarget = e.target === primary.target && e.targetHandle === primary.targetHandle
        if (sharesSource || sharesTarget) siblingIds.add(e.id)
      })
    }
    const updated = edges.map(e => {
      if (e.id === edgeId) return { ...e, data: { ...(e.data || {}), ...changes } }
      if (siblingIds.has(e.id)) return { ...e, data: { ...(e.data || {}), handle_color: changes.handle_color } }
      return e
    })
    handleGraphChange(nodes, updated)
    setSelectedEdges(prev => prev.map(se => {
      if (se.id === edgeId) return { ...se, data: { ...(se.data || {}), ...changes } }
      if (siblingIds.has(se.id)) return { ...se, data: { ...(se.data || {}), handle_color: changes.handle_color } }
      return se
    }))
  }

  // Applies the SAME change to several edges in one pass — used by the
  // Properties panel's group view (a fan-out/fan-in handle's "hide all N"
  // checkbox). Deliberately not "call handleEdgeUpdate once per edge": each
  // call there reads `edges` from this render's closure, so N sequential
  // calls in one event handler would each compute their update from the
  // same pre-loop snapshot and clobber one another — only the last one
  // would actually stick. Recomputing the whole array once here avoids that.
  function handleEdgesBulkUpdate(edgeIds, changes) {
    const idSet = new Set(edgeIds)
    const updated = edges.map(e => idSet.has(e.id) ? { ...e, data: { ...(e.data || {}), ...changes } } : e)
    handleGraphChange(nodes, updated)
    setSelectedEdges(prev => prev.map(se => idSet.has(se.id) ? { ...se, data: { ...(se.data || {}), ...changes } } : se))
  }

  // Rename a pipeline node. The node's name is its id: it keys the yaml
  // entry, the cache dir and the `nodes.<name>.<output>` references, so a
  // rename must update the id, the edges and any typed-in references — a
  // label-only change would revert on the next page load.
  const renameNode = useCallback((oldId, rawLabel) => {
    // Names appear in `nodes.<name>.<output>` refs matched as \w+ — keep
    // them identifier-safe, and never collide with an existing node.
    const newId = String(rawLabel || '').trim().replace(/\W/g, '_')
    if (!newId || newId === oldId) return
    if (nodes.some(n => n.id === newId)) return

    const refPrefix = `nodes.${oldId}.`
    const updatedNodes = nodes.map(n => {
      let data = n.data
      const iv = data?.input_values
      if (iv && Object.values(iv).some(v => typeof v === 'string' && v.startsWith(refPrefix))) {
        const rewritten = {}
        for (const [k, v] of Object.entries(iv)) {
          rewritten[k] = (typeof v === 'string' && v.startsWith(refPrefix))
            ? `nodes.${newId}.${v.slice(refPrefix.length)}`
            : v
        }
        data = { ...data, input_values: rewritten }
      }
      if (n.id === oldId) return { ...n, id: newId, data: { ...data, label: newId } }
      return data === n.data ? n : { ...n, data }
    })
    const updatedEdges = edges.map(e => {
      if (e.source !== oldId && e.target !== oldId) return e
      const source = e.source === oldId ? newId : e.source
      const target = e.target === oldId ? newId : e.target
      return { ...e, source, target, id: `e_${source}_${target}_${e.sourceHandle}_${e.targetHandle}` }
    })
    handleGraphChange(updatedNodes, updatedEdges)
    if (selectedNode?.id === oldId) {
      setSelectedNode(prev => ({ ...prev, id: newId, data: { ...prev.data, label: newId } }))
    }
    if (selectedEdges.some(se => se.source === oldId || se.target === oldId)) {
      setSelectedEdges(prev => prev.map(se => {
        const source = se.source === oldId ? newId : se.source
        const target = se.target === oldId ? newId : se.target
        return { ...se, source, target, id: `e_${source}_${target}_${se.sourceHandle}_${se.targetHandle}` }
      }))
    }
  }, [nodes, edges, handleGraphChange, selectedNode, selectedEdges])

  // ── Group ports (user-defined group inputs/outputs) ────────────────────────
  // action: { type: 'add'|'remove'|'rename', kind: 'input'|'output',
  //           name?, newName? }
  // Removing or renaming a port must also fix the edges attached to its
  // handle, or they would dangle / detach.
  const handleGroupPortsChange = useCallback((groupId, action) => {
    const group = nodes.find(n => n.id === groupId && n.type === 'groupNode')
    if (!group) return
    const key = action.kind === 'input' ? 'group_inputs' : 'group_outputs'
    const prefix = action.kind === 'input' ? 'gin-' : 'gout-'
    const list = group.data?.[key] || []
    let newList = list
    let newEdges = edges

    if (action.type === 'add') {
      const base = action.kind === 'input' ? 'in' : 'out'
      let i = 1
      while (list.includes(`${base}_${i}`)) i++
      newList = [...list, `${base}_${i}`]
    } else if (action.type === 'remove') {
      newList = list.filter(n => n !== action.name)
      const h = prefix + action.name
      newEdges = edges.filter(e =>
        !((e.source === groupId && e.sourceHandle === h) ||
          (e.target === groupId && e.targetHandle === h)))
    } else if (action.type === 'rename') {
      // Port names become yaml keys and handle ids — keep them
      // identifier-safe and unique within the group & kind.
      const clean = String(action.newName || '').trim().replace(/\W/g, '_')
      if (!clean || clean === action.name || list.includes(clean)) return
      newList = list.map(n => (n === action.name ? clean : n))
      const oldH = prefix + action.name
      const newH = prefix + clean
      newEdges = edges.map(e => {
        let out = e
        if (out.source === groupId && out.sourceHandle === oldH) out = { ...out, sourceHandle: newH }
        if (out.target === groupId && out.targetHandle === oldH) out = { ...out, targetHandle: newH }
        return out
      })
    }

    const newNodes = nodes.map(n =>
      n.id === groupId ? { ...n, data: { ...n.data, [key]: newList } } : n)
    handleGraphChange(newNodes, newEdges)
    if (selectedNode?.id === groupId) {
      setSelectedNode(prev => ({ ...prev, data: { ...prev.data, [key]: newList } }))
    }
  }, [nodes, edges, handleGraphChange, selectedNode])

  const handleNodeRename = useCallback((nodeId, newLabel) => {
    const target = nodes.find(n => n.id === nodeId)
    if (target && target.type === 'groupNode') {
      // Group labels are persisted as labels (the group id stays) — no
      // id rename needed.
      const updated = nodes.map(n =>
        n.id === nodeId ? { ...n, data: { ...n.data, label: newLabel } } : n
      )
      handleGraphChange(updated, edges)
      return
    }
    renameNode(nodeId, newLabel)
  }, [nodes, edges, handleGraphChange, renameNode])

  // -------------------------------------------------------------------------
  // Adding nodes & group components
  // -------------------------------------------------------------------------
  function uniqueGraphId(base, taken) {
    let id = base, i = 2
    while (taken.has(id)) id = `${base}_${i++}`
    taken.add(id)
    return id
  }

  // Expand an already-fetched group-component template into a fresh group +
  // child nodes with unique ids (gid is chosen by the caller); internal refs
  // and port links are rewritten to the new ids. Pure/synchronous — no
  // fetch, no state mutation — so callers can compose several expansions
  // (e.g. a node plus its auto-instantiated underlying groups) into one
  // handleGraphChange.
  function expandGroupTemplate(tpl, gid, taken, pos) {
    // Only children whose base type is installed can be created.
    const tplNodes = (tpl.nodes || []).filter(tn =>
      nodeTypes.some(t => t.type === tn.type && !t.is_group))
    const missing = (tpl.nodes || []).filter(tn => !tplNodes.includes(tn))
    const idMap = {}
    tplNodes.forEach(tn => { idMap[tn.name] = uniqueGraphId(tn.name, taken) })

    const refRe = /^nodes\.(\w+)\.(\w+)$/
    const newNodes = [{
      id: gid,
      type: 'groupNode',
      position: pos,
      style: { width: tpl.width || 360, height: tpl.height || 280 },
      data: {
        label: tpl.label || gid,
        color: tpl.color || '#2d4a6b',
        folded: false,
        description: tpl.description || '',
        group_inputs: tpl.inputs || [],
        group_outputs: tpl.outputs || [],
      },
    }]
    const newEdges = []

    tplNodes.forEach(tn => {
      const bt = nodeTypes.find(t => t.type === tn.type && !t.is_group)
      const nid = idMap[tn.name]
      const input_values = {}
      ;(tn.inputs || []).forEach(inp => {
        if (!('value' in inp) || inp.value == null) return
        let v = inp.value
        const m = typeof v === 'string' ? v.match(refRe) : null
        if (m && idMap[m[1]]) {
          v = `nodes.${idMap[m[1]]}.${m[2]}`
          newEdges.push({
            id: `e_${idMap[m[1]]}_${nid}_${m[2]}_${inp.name}`,
            source: idMap[m[1]], sourceHandle: m[2],
            target: nid, targetHandle: inp.name,
          })
        }
        input_values[inp.name] = v
      })
      newNodes.push({
        id: nid,
        type: 'ioNode',
        parentNode: gid,
        extent: 'parent',
        position: { x: tn.position?.[0] ?? 20, y: tn.position?.[1] ?? 40 },
        data: {
          label: nid,
          node_type: tn.type,
          inputs: bt.inputs || [],
          outputs: bt.outputs || [],
          input_values,
          color: tn.color || null,
          state: 'PENDING',
          is_kwui: !!bt.kwui,
        },
      })
    })

    ;(tpl.group_links || []).forEach(l => {
      const s = l.source === '__group__' ? gid : idMap[l.source]
      const t = l.target === '__group__' ? gid : idMap[l.target]
      if (!s || !t) return
      newEdges.push({
        id: `gl_${s}_${t}_${l.sourceHandle}_${l.targetHandle}`,
        source: s, sourceHandle: l.sourceHandle,
        target: t, targetHandle: l.targetHandle,
      })
    })

    // Jump links between two children are always internal-to-internal
    // (a group's own boundary jump hookups travel via tpl.group_links's
    // '__group__' entries instead, handled above) — just remap through
    // idMap like any other internal edge.
    ;(tpl.jump_links || []).forEach(l => {
      const s = idMap[l.source], t = idMap[l.target]
      if (!s || !t) return
      newEdges.push({
        id: `jl_${s}_${t}`,
        source: s, sourceHandle: 'jump-out',
        target: t, targetHandle: 'jump-in',
      })
    })

    return { newNodes, newEdges, missing }
  }

  // Expand an exported group component into a fresh group + child nodes with
  // unique ids; internal refs and port links are rewritten to the new ids.
  function addGroupComponent(nt, pos) {
    axios.get(`${API}/group-component`, { params: { category: nt.group_category || '', name: nt.group_name } })
      .then(r => {
        const tpl = r.data || {}
        const taken = new Set(nodes.map(n => n.id))
        const gid = uniqueGraphId(tpl.name || nt.group_name, taken)
        const { newNodes, newEdges, missing } = expandGroupTemplate(tpl, gid, taken, pos)

        handleGraphChange([...nodes, ...newNodes], [...edges, ...newEdges])
        if (missing.length) {
          setLogs(prev => [...prev, {
            text: `Group component: skipped nodes with missing base types: ${missing.map(m => `${m.name} (${m.type})`).join(', ')}`,
            cls: 'log-warn',
          }])
        }
      })
      .catch(() => setLogs(prev => [...prev, { text: 'Could not load group component.', cls: 'log-error' }]))
  }

  // Final step of node creation, once every underlying_groups slot with an
  // `address` has been resolved (found & auto-instantiated, or explicitly
  // ignored via the groupAddressPrompt dialog). `resolved` = [{slot,
  // category, name, tpl}] for slots whose group component was found;
  // `skipped` = plain slot-name strings the user chose to leave unbound.
  function createNodeAndGroups(nt, pos, resolved, skipped) {
    const type = nt.type
    const count = nodes.filter(n => n.data?.node_type === type).length
    const id = `${type}_${count + 1}`
    const taken = new Set(nodes.map(n => n.id))
    taken.add(id)

    const allNewNodes = []
    const allNewEdges = []
    const boundByName = {}

    resolved.forEach(({ slot, tpl }, i) => {
      const gid = uniqueGraphId(tpl.name || slot.name, taken)
      const { newNodes, newEdges, missing } = expandGroupTemplate(
        tpl, gid, taken, { x: pos.x, y: pos.y + 220 * (i + 1) })
      // A slot's auto-instantiated group is bound immediately, but follows
      // the same "starts disabled/unhighlighted until its chip is clicked"
      // default as any other bound slot (handleBindUnderlyingGroup does the
      // same for a manual binding).
      const grpNode = newNodes.find(n => n.id === gid)
      if (grpNode) { grpNode.data.disabled = true; grpNode.data.highlight_color = null }
      allNewNodes.push(...newNodes)
      allNewEdges.push(...newEdges)
      boundByName[slot.name] = { group_id: gid, label: tpl.label || gid }
      if (missing.length) {
        setLogs(prev => [...prev, {
          text: `Group '${gid}' (slot '${slot.name}'): skipped nodes with missing base types: ${missing.map(m => `${m.name} (${m.type})`).join(', ')}`,
          cls: 'log-warn',
        }])
      }
    })

    const newNode = {
      id,
      type: 'ioNode',
      position: pos,
      data: {
        label: id,
        node_type: type,
        inputs: nt.inputs || [],
        outputs: nt.outputs || [],
        input_values: nt.default || {},
        color: null,
        state: 'PENDING',
        // Type-level marker (from node_config.yaml's `kwui:`), set here too so
        // a freshly dropped node is badged immediately — not only after the
        // next /api/graph load, which is where it otherwise comes from.
        is_kwui: !!nt.kwui,
        // Same reasoning as is_kwui above: declared group slots so the
        // Properties panel's "Underlying Groups" section shows up
        // immediately instead of only after a reload. A slot with an
        // `address` that resolved to a real component is bound right away;
        // every other slot (no address, or the user chose "Ignore") starts
        // unbound, same as before.
        underlying_groups: (nt.underlying_groups || []).map(slot => {
          const b = boundByName[slot.name]
          return b
            ? { name: slot.name, description: slot.description || '', group_id: b.group_id, label: b.label }
            : { name: slot.name, description: slot.description || '', group_id: null, label: null }
        }),
      },
    }
    handleGraphChange([...nodes, newNode, ...allNewNodes], [...edges, ...allNewEdges])
  }

  // Walks a node type's declared underlying_groups slots in order. A slot
  // with no `address` is left as an empty/unbound slot, unchanged from
  // before. A slot WITH an address is checked against the group-component
  // store: found -> queued for auto-instantiation, silently; not found ->
  // groupAddressPrompt asks the user to ignore (create the node, that slot
  // stays unbound) or cancel (nothing is created at all, per-slot, in
  // sequence — matches the existing missingPrompt/confirmRun pattern of a
  // single nullable state object driving one dialog at a time).
  async function advanceSlotQueue(nt, pos, queue, resolved, skipped) {
    if (!queue.length) {
      createNodeAndGroups(nt, pos, resolved, skipped)
      return
    }
    const [slot, ...rest] = queue
    if (!slot.address) {
      advanceSlotQueue(nt, pos, rest, resolved, skipped)
      return
    }
    const idx = slot.address.lastIndexOf('/')
    const category = idx >= 0 ? slot.address.slice(0, idx) : ''
    const name = idx >= 0 ? slot.address.slice(idx + 1) : slot.address
    try {
      const r = await axios.get(`${API}/group-component`, { params: { category, name } })
      advanceSlotQueue(nt, pos, rest, [...resolved, { slot, category, name, tpl: r.data || {} }], skipped)
    } catch {
      setGroupAddressPrompt({ nt, pos, queue: rest, resolved, skipped, slot, category, name })
    }
  }

  function addNodeOfType(type, pos) {
    const nt = nodeTypes.find(n => n.type === type)
    if (!nt) return
    if (nt.is_group) {
      addGroupComponent(nt, pos)
      return
    }
    const slots = nt.underlying_groups || []
    if (!slots.some(s => s.address)) {
      createNodeAndGroups(nt, pos, [], [])
      return
    }
    advanceSlotQueue(nt, pos, slots, [], [])
  }

  // Every regular (non-group) node inside `groupId`, nested groups included,
  // split into those whose cache a group-wide clean may touch and frozen ones
  // (frozen nodes keep their cache, same rule as `kpl group clean`).
  function collectGroupMembers(groupId) {
    const ids = [], frozen = []
    const walk = gid => nodes.filter(n => n.parentNode === gid).forEach(n => {
      if (n.type === 'groupNode') walk(n.id)
      else (n.data?.frozen ? frozen : ids).push(n.id)
    })
    walk(groupId)
    return { ids, frozen }
  }

  // Export the selected group as a reusable component; on success refresh the
  // node types so it appears in the components panel right away.
  function handleExportGroup(groupId, opts) {
    return axios.post(`${API}/export-group`, { group_id: groupId, ...opts, nodes, edges })
      .then(r => {
        axios.get(`${API}/nodes`).then(res => setNodeTypes(res.data)).catch(() => {})
        return r.data
      })
  }

  // -------------------------------------------------------------------------
  // Queue info
  // -------------------------------------------------------------------------
  const pendingCount = Object.values(nodeStates).filter(s => s === 'PENDING').length
  const runningCount = Object.values(nodeStates).filter(s => s === 'RUNNING').length

  return (
    <div
      className="app-layout"
      style={{
        gridTemplateColumns: `${panelVisible.left ? panelSizes.left : 0}px 1fr ${panelVisible.right ? panelSizes.right : 0}px`,
        gridTemplateRows: `48px 1fr ${panelVisible.bottom ? panelSizes.bottom : 0}px`,
      }}
    >
      <div className="toolbar-row">
        <Toolbar
          isRunning={isRunning}
          wsConnected={wsConnected}
          selectedNode={selectedNode}
          onRun={() => handleRun(false)}
          onRunDebug={() => handleRun(true)}
          onStop={handleStop}
          onReset={handleReset}
          onClearCache={handleClearCache}
          onRunSelected={() => handleRun(false)}
          panelVisible={panelVisible}
          onTogglePanel={togglePanel}
        />
      </div>

      {panelVisible.left && (
        <div className="component-panel panel">
          <button className="panel-close-btn" title="Hide Components panel" onClick={() => togglePanel('left')}>✕</button>
          <ComponentPanel
            nodeTypes={nodeTypes}
            onAddNode={(type) => addNodeOfType(type, { x: 100 + Math.random() * 200, y: 100 + Math.random() * 200 })}
          />
        </div>
      )}
      {panelVisible.left && (
        <ResizeHandle orientation="vertical" style={{ left: panelSizes.left, bottom: panelVisible.bottom ? panelSizes.bottom : 0 }} onResize={resizeLeft} />
      )}

      <div className="graph-panel">
        <GraphPanel
          nodes={nodes}
          edges={edges}
          nodeStates={nodeStates}
          externalRunning={externalRunning}
          nodeTypes={nodeTypes}
          guiConfig={guiConfig}
          onGraphChange={handleGraphChange}
          onSelectNode={setSelectedNode}
          onSelectEdges={setSelectedEdges}
          selectedNode={selectedNode}
          selectedEdges={selectedEdges}
          onNodeRename={handleNodeRename}
          onRunNode={(nid) => {
            setSelectedNode(nodes.find(n => n.id === nid) || null)
            setSelectedEdges([])
            requestNodeRun(nid, false)
          }}
          // GraphPanel now owns node/group deletion itself (cascade-deletes a
          // group's children instead of orphaning them, and confirms first) —
          // this only clears App-level selection for whichever id(s) it removed.
          onDeleteNode={(nid) => {
            if (selectedNode?.id === nid) setSelectedNode(null)
            if (selectedEdges.some(se => se.source === nid || se.target === nid)) {
              setSelectedEdges(prev => prev.filter(se => se.source !== nid && se.target !== nid))
            }
          }}
          onAddGroupNode={(pos) => {
            const id = `group_${Date.now()}`
            const grp = {
              id,
              type: 'groupNode',
              position: pos || { x: 80, y: 80 },
              style: { width: 360, height: 280 },
              data: { label: id, color: '#2d4a6b', folded: false, group_inputs: [], group_outputs: [] },
            }
            handleGraphChange([...nodes, grp], edges)
          }}
          onAddNodeAtPosition={addNodeOfType}
          onToggleIOFlag={(nid, key) => {
            const n = nodes.find(x => x.id === nid)
            if (n && n.type !== 'groupNode') handleNodeUpdate(nid, { [key]: !n.data?.[key] })
          }}
        />
      </div>

      {panelVisible.right && (
        <ResizeHandle orientation="vertical" style={{ right: panelSizes.right, bottom: panelVisible.bottom ? panelSizes.bottom : 0 }} onResize={resizeRight} />
      )}
      {panelVisible.right && (
        <div className="properties-panel panel">
          <button className="panel-close-btn" title="Hide Properties panel" onClick={() => togglePanel('right')}>✕</button>
          <PropertiesPanel
            node={selectedNode}
            selectedEdges={selectedEdges}
            nodeTypes={nodeTypes}
            edges={edges}
            availableGroups={nodes.filter(n => n.type === 'groupNode').map(n => ({ id: n.id, label: n.data?.label || n.id }))}
            groupMembers={selectedNode?.type === 'groupNode' ? collectGroupMembers(selectedNode.id) : null}
            isRunning={isRunning}
            nodeState={selectedNode ? nodeStates[selectedNode.id] : null}
            onUpdate={(changes) => selectedNode && handleNodeUpdate(selectedNode.id, changes)}
            onBindUnderlyingGroup={(slotName, groupId) => selectedNode && handleBindUnderlyingGroup(selectedNode.id, slotName, groupId)}
            onUpdateEdge={handleEdgeUpdate}
            onUpdateEdgesBulk={handleEdgesBulkUpdate}
            onGroupPortsChange={(action) => selectedNode && handleGroupPortsChange(selectedNode.id, action)}
            onExportGroup={(opts) => selectedNode ? handleExportGroup(selectedNode.id, opts) : Promise.reject()}
            onRun={() => selectedNode && handleRun(false)}
            onRunDebug={() => selectedNode && handleRun(true)}
            onRunGroup={() => selectedNode && executeGroupRun(selectedNode.id)}
          />
        </div>
      )}

      {missingPrompt && (
        <div className="confirm-overlay">
          <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
            <div className="confirm-text">
              The following nodes were not found in the base nodes:
              <ul style={{ margin: '8px 0 0 18px', textAlign: 'left' }}>
                {missingPrompt.missing.map(m => (
                  <li key={m.name}>{m.name} <span style={{ color: '#718096' }}>({m.type})</span></li>
                ))}
              </ul>
              <div style={{ marginTop: '10px' }}>
                Ignore them and load the graph anyway?
              </div>
            </div>
            <div className="confirm-buttons">
              <button
                className="btn btn-primary"
                autoFocus
                onClick={() => {
                  // Load without saving — saving here would silently drop the
                  // missing nodes from graph_config.yaml.
                  setNodes(missingPrompt.nodes)
                  setEdges(missingPrompt.edges)
                  setMissingPrompt(null)
                }}
              >
                Ignore & Load
              </button>
              <button
                className="btn btn-cancel"
                onClick={() => setMissingPrompt(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {groupAddressPrompt && (
        <div className="confirm-overlay">
          <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
            <div className="confirm-text">
              No group component found at '{groupAddressPrompt.category ? `${groupAddressPrompt.category}/` : ''}{groupAddressPrompt.name}'
              {' '}for slot '{groupAddressPrompt.slot.name}'.
              {groupAddressPrompt.slot.description && (
                <div style={{ marginTop: '6px', color: '#a0aec0', fontSize: '12px' }}>
                  {groupAddressPrompt.slot.description}
                </div>
              )}
              <div style={{ marginTop: '10px' }}>
                Ignore it and create the node anyway (slot left unbound), or cancel creating the node?
              </div>
            </div>
            <div className="confirm-buttons">
              <button
                className="btn btn-primary"
                autoFocus
                onClick={() => {
                  const { nt, pos, queue, resolved, skipped, slot } = groupAddressPrompt
                  setGroupAddressPrompt(null)
                  advanceSlotQueue(nt, pos, queue, resolved, [...skipped, slot.name])
                }}
              >
                Ignore & Continue
              </button>
              <button
                className="btn btn-cancel"
                onClick={() => setGroupAddressPrompt(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {confirmRun && (
        <div className="confirm-overlay" onClick={() => setConfirmRun(null)}>
          <div className="confirm-dialog" onClick={e => e.stopPropagation()}>
            <div className="confirm-text">
              Remove cache of '{confirmRun.nodeId}' before running?
            </div>
            <div className="confirm-buttons">
              <button
                className="btn btn-primary"
                autoFocus
                onClick={() => executeRun(confirmRun.nodeId, confirmRun.debug, true)}
              >
                Yes
              </button>
              <button
                className="btn"
                onClick={() => executeRun(confirmRun.nodeId, confirmRun.debug, false)}
              >
                No
              </button>
              <button
                className="btn btn-cancel"
                onClick={() => setConfirmRun(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {panelVisible.bottom && (
        <ResizeHandle orientation="horizontal" style={{ bottom: panelSizes.bottom }} onResize={resizeBottom} />
      )}
      {panelVisible.bottom && (
        <div className="terminal-row">
          <button className="panel-close-btn" title="Hide Terminal panel" onClick={() => togglePanel('bottom')}>✕</button>
          <TerminalPanel
            logs={logs}
            progress={progress}
            systemStats={systemStats}
            isRunning={isRunning}
            pendingCount={pendingCount}
            runningCount={runningCount}
          />
        </div>
      )}
    </div>
  )
}
