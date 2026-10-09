import { useState, useEffect, useRef } from 'react'
import axios from 'axios'

function formatSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

// Modal that browses the pipeline's data/ directory so a FILE input can be
// pointed at one of its files, with an upload (+ progress / cancel) option.
function FilePickerModal({ onSelect, onClose }) {
  const [path, setPath] = useState('')
  const [entries, setEntries] = useState([])
  const [error, setError] = useState('')
  const [upload, setUpload] = useState(null) // { name, pct }
  const abortRef = useRef(null)
  const fileInputRef = useRef(null)

  function load(p) {
    axios.get('/api/data-files', { params: { path: p } })
      .then(r => {
        setPath(r.data.path || '')
        setEntries(r.data.entries || [])
        setError('')
      })
      .catch(() => setError('Could not read data directory.'))
  }

  useEffect(() => { load('') }, [])

  function goUp() {
    const parts = path.split('/').filter(Boolean)
    parts.pop()
    load(parts.join('/'))
  }

  function handleUploadPick(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    const controller = new AbortController()
    abortRef.current = controller
    setUpload({ name: file.name, pct: 0 })
    axios.post('/api/upload-data', file, {
      params: { filename: file.name, path },
      headers: { 'Content-Type': 'application/octet-stream' },
      signal: controller.signal,
      onUploadProgress: ev => {
        setUpload({ name: file.name, pct: ev.total ? Math.round((ev.loaded / ev.total) * 100) : 0 })
      },
    })
      .then(r => {
        setUpload(null)
        // Uploaded file becomes the selected value automatically.
        onSelect(r.data.path)
      })
      .catch(err => {
        setUpload(null)
        if (!axios.isCancel(err)) setError('Upload failed.')
        load(path)
      })
  }

  function cancelUpload() {
    abortRef.current?.abort()
  }

  return (
    <div className="confirm-overlay" onClick={upload ? undefined : onClose}>
      <div
        className="confirm-dialog"
        onClick={e => e.stopPropagation()}
        style={{ width: 420, maxWidth: '90vw', textAlign: 'left' }}
      >
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8 }}>
          Select file — data/{path}
        </div>

        {upload && (
          <div style={{ marginBottom: 8 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 6, background: '#2d3748', borderRadius: 3, overflow: 'hidden' }}>
                <div style={{ width: `${upload.pct}%`, height: '100%', background: '#2b6cb0', transition: 'width .15s' }} />
              </div>
              <span style={{ fontSize: 11, color: '#a0aec0', minWidth: 34, textAlign: 'right' }}>{upload.pct}%</span>
              <button className="btn btn-cancel" style={{ fontSize: 11, padding: '2px 8px' }} onClick={cancelUpload}>
                Cancel
              </button>
            </div>
            <div style={{ fontSize: 11, color: '#718096', marginTop: 2 }}>Uploading {upload.name}…</div>
          </div>
        )}

        {error && <div style={{ fontSize: 11, color: '#fc8181', marginBottom: 6 }}>{error}</div>}

        <div style={{
          maxHeight: 260, overflowY: 'auto',
          border: '1px solid #2d3748', borderRadius: 4, marginBottom: 10,
        }}>
          {path && (
            <div className="comp-node-item" onClick={goUp} style={{ cursor: 'pointer' }}>
              <span className="node-icon">↩</span>..
            </div>
          )}
          {entries.map(e => (
            <div
              key={e.name}
              className="comp-node-item"
              style={{ cursor: 'pointer', display: 'flex', gap: 6 }}
              onClick={() => e.is_dir ? load(path ? `${path}/${e.name}` : e.name) : onSelect(e.path)}
              title={e.path}
            >
              <span className="node-icon">{e.is_dir ? '📁' : '📄'}</span>
              <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.name}</span>
              <span style={{ color: '#718096', fontSize: 10 }}>{formatSize(e.size)}</span>
            </div>
          ))}
          {entries.length === 0 && !path && (
            <div style={{ padding: 12, fontSize: 11, color: '#718096' }}>data/ is empty.</div>
          )}
        </div>

        <div className="confirm-buttons">
          <button
            className="btn btn-primary"
            disabled={!!upload}
            onClick={() => fileInputRef.current?.click()}
          >
            ⬆ Upload
          </button>
          <button className="btn btn-cancel" disabled={!!upload} onClick={onClose}>Close</button>
        </div>
        <input type="file" ref={fileInputRef} style={{ display: 'none' }} onChange={handleUploadPick} />
      </div>
    </div>
  )
}

// One row in the group-port editor: rename on blur/Enter, delete via ✕.
function GroupPortRow({ kind, name, onChange }) {
  const [val, setVal] = useState(name)
  useEffect(() => setVal(name), [name])

  function commit() {
    if (val !== name) onChange({ type: 'rename', kind, name, newName: val })
  }

  return (
    <div className="props-field" style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#d69e2e', flexShrink: 0 }} />
      <input
        style={{ flex: 1, minWidth: 0 }}
        value={val}
        onChange={e => setVal(e.target.value)}
        onBlur={commit}
        onKeyDown={e => e.key === 'Enter' && e.target.blur()}
      />
      <button
        className="btn"
        title={`Delete ${kind} '${name}'`}
        style={{ padding: '2px 7px', fontSize: 11, flexShrink: 0 }}
        onClick={() => onChange({ type: 'remove', kind, name })}
      >
        ✕
      </button>
    </div>
  )
}

// Group-port editor: the group's user-defined inputs or outputs.
function GroupPortsSection({ kind, ports, onChange }) {
  const title = kind === 'input' ? 'Group Inputs' : 'Group Outputs'
  return (
    <div className="props-section">
      <div className="props-section-title">{title}</div>
      {ports.length === 0 && (
        <div style={{ fontSize: 11, color: '#718096', marginBottom: 4 }}>
          No {kind}s defined.
        </div>
      )}
      {ports.map(name => (
        <GroupPortRow key={name} kind={kind} name={name} onChange={onChange} />
      ))}
      <button
        className="btn"
        style={{ fontSize: 11, padding: '2px 8px', marginTop: 2 }}
        onClick={() => onChange({ type: 'add', kind })}
      >
        + Add {kind}
      </button>
    </div>
  )
}

// Export the selected group as a reusable component. The group's label is
// the component name; only the category is chosen here. A 409 from the
// server means the component exists — ask before overwriting.
function GroupExportSection({ node, nodeTypes, onExportGroup }) {
  const [category, setCategory] = useState('')
  const [msg, setMsg] = useState('')

  useEffect(() => setMsg(''), [node.id])

  const name = node.data?.label || node.id

  const existingCats = [...new Set(
    (nodeTypes || []).filter(t => t.is_group && t.group_category).map(t => t.group_category)
  )]

  function doExport(overwrite) {
    setMsg('')
    onExportGroup({ name, category, overwrite })
      .then(r => setMsg(r?.path ? `Exported to ${r.path}` : 'Exported.'))
      .catch(err => {
        if (err?.response?.status === 409) {
          if (window.confirm(`${err.response.data?.msg || 'Component exists.'} Overwrite?`)) doExport(true)
        } else {
          setMsg(err?.response?.data?.msg || 'Export failed.')
        }
      })
  }

  return (
    <div className="props-section">
      <div className="props-section-title">Export as Component</div>
      <div className="props-field">
        <label>Name (group label)</label>
        <div style={{ fontSize: 12, color: '#e2e8f0', padding: '2px 0' }}>{name}</div>
      </div>
      <div className="props-field">
        <label>Category</label>
        <input
          value={category}
          onChange={e => setCategory(e.target.value)}
          placeholder="category…"
          list="group-export-categories"
        />
        <datalist id="group-export-categories">
          {existingCats.map(c => <option key={c} value={c} />)}
        </datalist>
      </div>
      <button
        className="btn btn-primary props-run-btn"
        onClick={() => doExport(false)}
      >
        ⬆ Export Group
      </button>
      {msg && <div style={{ marginTop: 4, fontSize: 11, color: '#a0aec0', wordBreak: 'break-all' }}>{msg}</div>}
    </div>
  )
}

const NODE_COLORS = [
  '#2d4a6b', '#3d4f2b', '#4a2b2b', '#2b3a4a', '#3a2b4a',
  '#4a3a2b', '#1a3a3a', '#2b2b4a',
]

const REF_RE = /^nodes\.(\w+)\.(\w+)$/

// Random handle color: clamped away from near-black/near-white so it stays
// visible against both the handle's dark fill and its light hover state.
function randomHandleColor() {
  const c = () => Math.floor(80 + Math.random() * 176)
  const hex = n => n.toString(16).padStart(2, '0')
  return `#${hex(c())}${hex(c())}${hex(c())}`
}

// Shared "Handle Color" picker (swatch row + custom color + random/reset) —
// used both for a single link and for a fan-out/fan-in group of links, since
// the control itself is identical either way (see HandleColorSection's
// callers for what onPick(null) — Reset — means in each case).
function HandleColorSection({ color, onPick, title = 'Handle Color' }) {
  return (
    <div className="props-section">
      <div className="props-section-title">{title}</div>
      <div className="props-color-row">
        <input
          type="color"
          value={color || '#63b3ed'}
          onChange={e => onPick(e.target.value)}
        />
        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          {NODE_COLORS.map(c => (
            <div
              key={c}
              onClick={() => onPick(c)}
              style={{
                width: 16, height: 16,
                borderRadius: 3,
                background: c,
                cursor: 'pointer',
                border: color === c ? '2px solid #63b3ed' : '1px solid #4a5568',
              }}
            />
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
        <button
          className="btn"
          style={{ fontSize: 11, padding: '2px 8px' }}
          onClick={() => onPick(randomHandleColor())}
        >
          🎲 Random
        </button>
        {color && (
          <button
            className="btn"
            style={{ fontSize: 11, padding: '2px 8px' }}
            onClick={() => onPick(null)}
          >
            Reset
          </button>
        )}
      </div>
    </div>
  )
}

// Properties for a single selected link (plain node-to-node edge, or a jump
// link — see GraphPanel's isSelectableEdge). handle_color tints the center
// of both connected handles (and, if either handle carries other links too,
// theirs as well — see App.jsx's handleEdgeUpdate); hide additionally hides
// the line itself and highlights the handles' outer ring instead (still
// clickable there to reselect the link).
function EdgeProperties({ edge, onUpdate }) {
  const data = edge.data || {}
  const color = data.handle_color || ''

  return (
    <>
      <div className="panel-header">Properties</div>
      <div className="props-node-title">
        <div style={{ fontWeight: 700, fontSize: 13 }}>Link</div>
        <div className="props-node-type">
          {edge.source}.{edge.sourceHandle} → {edge.target}.{edge.targetHandle}
        </div>
      </div>

      <HandleColorSection color={color} onPick={v => onUpdate({ handle_color: v })} />

      <div className="props-section">
        <div className="props-field">
          <label>Hide link</label>
          <input
            type="checkbox"
            checked={!!data.hide}
            onChange={e => onUpdate({ hide: e.target.checked })}
            style={{ width: 'auto' }}
          />
        </div>
        <div style={{ fontSize: 11, color: '#718096', marginTop: 2 }}>
          Hides the connecting line; its handles stay highlighted and clickable to reselect it.
        </div>
      </div>
    </>
  )
}

// Properties for several links selected together by clicking a fan-out/
// fan-in handle (GraphPanel's handleHandleClick) — every link on that handle
// is genuinely one shared color (there's only one handle dot to draw, no
// matter how many links touch it), so ONE color control here updates all of
// them at once (via onUpdateOne(edges[0].id, ...) — App.jsx's
// handleEdgeUpdate already propagates a handle_color change to every edge
// sharing that handle). Hide is a per-link data field, but the group view
// still offers just one checkbox that sets it on every link in the group at
// once — via onUpdateBulk (App.jsx's handleEdgesBulkUpdate, a single-pass
// update over all of them; looping the single-edge onUpdateOne here would
// silently lose all but the last one, since each call reads edges from the
// same pre-loop render's closure) — simpler than a checkbox list when a
// handle fans out to many links.
function GroupEdgeProperties({ edges: group, onUpdateOne, onUpdateBulk }) {
  const color = group[0]?.data?.handle_color || ''
  const allHidden = group.every(e => e.data?.hide)

  return (
    <>
      <div className="panel-header">Properties</div>
      <div className="props-node-title">
        <div style={{ fontWeight: 700, fontSize: 13 }}>{group.length} Links</div>
        <div className="props-node-type">sharing one handle</div>
      </div>

      <HandleColorSection
        color={color}
        onPick={v => onUpdateOne(group[0].id, { handle_color: v })}
        title={`Handle Color (all ${group.length})`}
      />

      <div className="props-section">
        {group.map(e => (
          <div key={e.id} className="props-node-type" style={{ marginBottom: 2 }}>
            {e.source}.{e.sourceHandle} → {e.target}.{e.targetHandle}
          </div>
        ))}
        <div className="props-field" style={{ marginTop: 6 }}>
          <label>Hide all {group.length}</label>
          <input
            type="checkbox"
            checked={allHidden}
            onChange={ev => onUpdateBulk(group.map(e => e.id), { hide: ev.target.checked })}
            style={{ width: 'auto' }}
          />
        </div>
        <div style={{ fontSize: 11, color: '#718096', marginTop: 2 }}>
          Hides all {group.length} connecting lines; their handles stay highlighted and clickable to reselect them.
        </div>
      </div>
    </>
  )
}

// Format a cached output value for display: primitives inline, anything
// else (list/dict from outputs.yaml) as pretty-printed JSON.
function formatOutputValue(val) {
  if (val === null || val === undefined) return ''
  if (typeof val === 'object') return JSON.stringify(val, null, 2)
  return String(val)
}

export default function PropertiesPanel({ node, selectedEdges, nodeTypes, edges, isRunning, nodeState, onUpdate, onUpdateEdge, onUpdateEdgesBulk, onGroupPortsChange, onExportGroup, onRun, onRunDebug, onRunGroup, availableGroups, onBindUnderlyingGroup, groupMembers }) {
  const [inputValues, setInputValues] = useState({})
  const [titleVal, setTitleVal] = useState('')
  const [descVal, setDescVal] = useState('')
  const [cacheMsg, setCacheMsg] = useState('')
  const [filePicker, setFilePicker] = useState(null) // input name being browsed
  const [cachedOutputs, setCachedOutputs] = useState(null) // outputs.yaml contents, or null if none
  const [outputsCollapsed, setOutputsCollapsed] = useState(true)

  useEffect(() => {
    if (node) {
      setInputValues(node.data?.input_values || {})
      setTitleVal(node.data?.label || node.id)
      setDescVal(node.data?.description || '')
      setCacheMsg('')
      setFilePicker(null)
    }
  }, [node?.id])

  // Load cached outputs for the selected node, re-fetching whenever the node
  // finishes a run so the panel reflects fresh values without a re-select.
  useEffect(() => {
    if (!node || node.type === 'groupNode') {
      setCachedOutputs(null)
      return
    }
    let cancelled = false
    axios.get(`/api/node-outputs/${encodeURIComponent(node.id)}`)
      .then(r => { if (!cancelled) setCachedOutputs(r.data?.cached ? r.data.outputs : null) })
      .catch(() => { if (!cancelled) setCachedOutputs(null) })
    return () => { cancelled = true }
  }, [node?.id, node?.type, nodeState])

  if (!node) {
    if (selectedEdges?.length > 1) {
      return <GroupEdgeProperties edges={selectedEdges} onUpdateOne={onUpdateEdge} onUpdateBulk={onUpdateEdgesBulk} />
    }
    if (selectedEdges?.length === 1) {
      const edge = selectedEdges[0]
      return <EdgeProperties edge={edge} onUpdate={changes => onUpdateEdge(edge.id, changes)} />
    }
    return (
      <>
        <div className="panel-header">Properties</div>
        <div className="props-empty">Select a node to view its properties.</div>
      </>
    )
  }

  const isGroup = node.type === 'groupNode'
  // Input types (STRING/INT/FILE/…) come from the base node's config, via /api/nodes.
  const inputTypes = nodeTypes?.find(t => t.type === node.data?.node_type)?.input_types || {}
  const inputDefaults = nodeTypes?.find(t => t.type === node.data?.node_type)?.default || {}

  // Build edge-based connection map: inputHandle → { source, sourceHandle }
  const edgeConnections = {}
  if (edges && node) {
    edges.forEach(e => {
      if (e.target === node.id && e.targetHandle) {
        edgeConnections[e.targetHandle] = { source: e.source, sourceHandle: e.sourceHandle }
      }
    })
  }

  function handleInputChange(name, value) {
    const updated = { ...inputValues, [name]: value }
    setInputValues(updated)
    onUpdate({ input_values: updated })
  }

  function handleTitleBlur() {
    if (titleVal !== node.data?.label) {
      onUpdate({ label: titleVal })
    }
  }

  function handleColorChange(color) {
    onUpdate({ color })
  }

  // Zip the node's cache dir on the server and download it. Check first that
  // there is a cache, so a missing one shows a message instead of a 404 page.
  function handleDownloadCache() {
    setCacheMsg('')
    axios.get(`/api/node-cache/${encodeURIComponent(node.id)}`)
      .then(r => {
        if (r.data?.cached) {
          const a = document.createElement('a')
          a.href = `/api/node-cache-zip/${encodeURIComponent(node.id)}`
          a.download = `${node.id}_cache.zip`
          document.body.appendChild(a)
          a.click()
          a.remove()
        } else {
          setCacheMsg('No cache for this node yet.')
        }
      })
      .catch(() => setCacheMsg('Could not check node cache.'))
  }

  // Delete the node's cache dir on the server (after a confirmation).
  function handleRemoveCache() {
    setCacheMsg('')
    axios.get(`/api/node-cache/${encodeURIComponent(node.id)}`)
      .then(r => {
        if (!r.data?.cached) {
          setCacheMsg('No cache for this node yet.')
          return
        }
        if (!window.confirm(`Remove the cache of '${node.id}'?`)) return
        axios.delete(`/api/node-cache/${encodeURIComponent(node.id)}`)
          .then(res => setCacheMsg(res.data?.msg || 'Cache removed.'))
          .catch(() => setCacheMsg('Could not remove node cache.'))
      })
      .catch(() => setCacheMsg('Could not check node cache.'))
  }

  // Group-wide version: remove the cache of every (non-frozen) node inside
  // the selected group, nested groups included.
  function handleRemoveGroupCaches() {
    setCacheMsg('')
    const ids = groupMembers?.ids || []
    if (ids.length === 0) {
      setCacheMsg('This group has no nodes with a clearable cache.')
      return
    }
    const skipped = groupMembers?.frozen?.length || 0
    const note = skipped ? ` (${skipped} frozen node${skipped > 1 ? 's' : ''} will be kept)` : ''
    if (!window.confirm(`Remove the cache of ${ids.length} node${ids.length > 1 ? 's' : ''} in '${node.id}'?${note}`)) return
    axios.post('/api/remove-caches', { node_ids: ids })
      .then(res => {
        const n = res.data?.removed?.length || 0
        setCacheMsg(res.data?.ok
          ? (n ? `Removed the cache of ${n} node${n > 1 ? 's' : ''}.` : 'No cache to remove in this group.')
          : 'Could not remove some caches.')
      })
      .catch(() => setCacheMsg('Could not remove group caches.'))
  }

  return (
    <>
      <div className="panel-header">Properties</div>

      <div className="props-node-title">
        <input
          style={{
            background: 'transparent',
            border: 'none',
            borderBottom: '1px solid #4a5568',
            color: '#e2e8f0',
            fontWeight: 700,
            fontSize: 13,
            width: '100%',
            outline: 'none',
          }}
          value={titleVal}
          onChange={e => setTitleVal(e.target.value)}
          onBlur={handleTitleBlur}
          onKeyDown={e => e.key === 'Enter' && e.target.blur()}
        />
        <div className="props-node-type">{node.data?.node_type || node.type}</div>
        {nodeState && (
          <span className={`state-badge state-${nodeState}`}>{nodeState}</span>
        )}
      </div>

      {/* Color picker */}
      <div className="props-section">
        <div className="props-section-title">Color</div>
        <div className="props-color-row">
          <input
            type="color"
            value={node.data?.color || '#2d3748'}
            onChange={e => handleColorChange(e.target.value)}
          />
          <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            {NODE_COLORS.map(c => (
              <div
                key={c}
                onClick={() => handleColorChange(c)}
                style={{
                  width: 16, height: 16,
                  borderRadius: 3,
                  background: c,
                  cursor: 'pointer',
                  border: node.data?.color === c ? '2px solid #63b3ed' : '1px solid #4a5568',
                }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Inputs */}
      {!isGroup && node.data?.inputs?.length > 0 && (
        <div className="props-section">
          <div className="props-section-title">Inputs</div>
          {node.data.inputs.map(inp => {
            const conn = edgeConnections[inp]
            const val = inputValues[inp]
            const refMatch = !conn && typeof val === 'string' ? val.match(REF_RE) : null

            if (conn) {
              // Connected via edge wire
              return (
                <div key={inp} className="props-field">
                  <label>{inp}</label>
                  <div className="props-conn-chip">
                    <span className="conn-arrow">↗</span>
                    <span className="conn-source">{conn.source}</span>
                    <span className="conn-dot">·</span>
                    <span className="conn-handle">{conn.sourceHandle}</span>
                  </div>
                </div>
              )
            } else if (refMatch) {
              // Value stored as nodes.X.Y reference
              return (
                <div key={inp} className="props-field">
                  <label>{inp}</label>
                  <div className="props-conn-chip">
                    <span className="conn-arrow">↗</span>
                    <span className="conn-source">{refMatch[1]}</span>
                    <span className="conn-dot">·</span>
                    <span className="conn-handle">{refMatch[2]}</span>
                  </div>
                </div>
              )
            } else if ((inputTypes[inp] || '').toUpperCase() === 'BOOL') {
              // Real checkbox, not a text field: keeps the value a genuine
              // boolean end to end (JSON -> graph_config.yaml -> argparse
              // store_true flag), so it round-trips through YAML as `true`/
              // `false` instead of being saved as a quoted string like
              // 'true' the moment the user touches the field.
              const raw = val !== undefined ? val : inputDefaults[inp]
              const checked = raw === true || String(raw).trim().toLowerCase() === 'true'
              return (
                <div key={inp} className="props-field">
                  <label>{inp}</label>
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={e => handleInputChange(inp, e.target.checked)}
                    style={{ width: 'auto' }}
                  />
                </div>
              )
            } else {
              // Plain editable value; FILE inputs get a browse button that
              // opens the data-dir file picker.
              const isFile = (inputTypes[inp] || '').toUpperCase() === 'FILE'
              return (
                <div key={inp} className="props-field">
                  <label>{inp}</label>
                  <div style={{ display: 'flex', gap: 4 }}>
                    <input
                      style={{ flex: 1, minWidth: 0 }}
                      value={val ?? ''}
                      onChange={e => handleInputChange(inp, e.target.value)}
                      placeholder="value…"
                    />
                    {isFile && (
                      <button
                        className="btn"
                        title="Select a file from data/"
                        style={{ padding: '2px 7px', fontSize: 11, flexShrink: 0 }}
                        onClick={() => setFilePicker(inp)}
                      >
                        📁
                      </button>
                    )}
                  </div>
                </div>
              )
            }
          })}
        </div>
      )}

      {/* Outputs (from cache/<uuid>/<node>/outputs.yaml, if it exists) */}
      {!isGroup && node.data?.outputs?.length > 0 && (
        <div className="props-section">
          <div
            className="props-section-title props-section-title-collapsible"
            onClick={() => setOutputsCollapsed(v => !v)}
          >
            <span>{outputsCollapsed ? '▶' : '▼'}</span>
            Outputs
          </div>
          {!outputsCollapsed && (cachedOutputs ? (
            node.data.outputs.map(out => (
              <div key={out} className="props-field">
                <label>{out}</label>
                <div
                  style={{
                    fontSize: 11, color: '#e2e8f0', background: '#1a202c',
                    border: '1px solid #2d3748', borderRadius: 4, padding: '3px 6px',
                    whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 120, overflowY: 'auto',
                  }}
                >
                  {out in cachedOutputs
                    ? (formatOutputValue(cachedOutputs[out]) || <span style={{ color: '#718096' }}>(empty)</span>)
                    : <span style={{ color: '#718096' }}>(not cached)</span>}
                </div>
              </div>
            ))
          ) : (
            <div style={{ fontSize: 11, color: '#718096' }}>No cached outputs yet — run this node.</div>
          ))}
        </div>
      )}

      {/* Underlying groups: bind each declared slot (node_config.yaml's
          underlying_groups) to an actual group in this pipeline — this is
          the only place a binding is SET. Selecting/highlighting an
          already-bound slot happens via its chip under the node on the
          canvas (IONode.jsx), not here. */}
      {!isGroup && node.data?.underlying_groups?.length > 0 && (
        <div className="props-section">
          <div className="props-section-title">Underlying Groups</div>
          {node.data.underlying_groups.map(slot => (
            <div key={slot.name} className="props-field">
              <label title={slot.description || undefined}>{slot.name}</label>
              <select
                value={slot.group_id || ''}
                onChange={e => onBindUnderlyingGroup(slot.name, e.target.value || null)}
              >
                <option value="">— none —</option>
                {(availableGroups || []).map(g => (
                  <option key={g.id} value={g.id}>{g.label}</option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}

      {/* Group settings */}
      {isGroup && (
        <>
          <div className="props-section">
            <div className="props-section-title">Group</div>
            <div className="props-field">
              <label>Folder view</label>
              <input
                type="checkbox"
                checked={node.data?.folded || false}
                onChange={e => onUpdate({ folded: e.target.checked })}
                style={{ width: 'auto' }}
              />
            </div>
            <div className="props-field">
              <label>Description</label>
              <textarea
                rows={3}
                style={{ width: '100%', resize: 'vertical', fontSize: 11 }}
                value={descVal}
                onChange={e => setDescVal(e.target.value)}
                onBlur={() => descVal !== (node.data?.description || '') && onUpdate({ description: descVal })}
                placeholder="Describe this group…"
              />
            </div>
          </div>
          <GroupPortsSection
            kind="input"
            ports={node.data?.group_inputs || []}
            onChange={onGroupPortsChange}
          />
          <GroupPortsSection
            kind="output"
            ports={node.data?.group_outputs || []}
            onChange={onGroupPortsChange}
          />
          <GroupExportSection
            node={node}
            nodeTypes={nodeTypes}
            onExportGroup={onExportGroup}
          />
          <div className="props-section">
            <button className="btn btn-primary props-run-btn" disabled={isRunning} onClick={onRunGroup}
              title="Runs whatever feeds this group's outputs (like self.groups['x'].run() in node code); cached upstream results are reused">
              ▶ Run This Group
            </button>
            <button className="btn props-run-btn" onClick={handleRemoveGroupCaches}
              style={{ marginTop: 4, fontSize: 11 }}
              title="Removes the cache of every node inside this group (frozen nodes are kept)">
              🗑 Remove Caches
            </button>
            {cacheMsg && (
              <div style={{ marginTop: 4, fontSize: 11, color: '#a0aec0' }}>{cacheMsg}</div>
            )}
          </div>
        </>
      )}

      {/* Run buttons */}
      {!isGroup && (
        <div className="props-section">
          <button className="btn btn-primary props-run-btn" disabled={isRunning} onClick={onRun}>
            ▶ Run This Node
          </button>
          <button className="btn btn-debug props-run-btn" disabled={isRunning} onClick={onRunDebug}
            style={{ marginTop: 4 }}>
            ⚙ Debug This Node
          </button>
          <button className="btn props-run-btn" onClick={handleDownloadCache}
            style={{ marginTop: 4, fontSize: 11 }}>
            ⬇ Download Outputs & Caches
          </button>
          <button className="btn props-run-btn" onClick={handleRemoveCache}
            style={{ marginTop: 4, fontSize: 11 }}>
            🗑 Remove Caches
          </button>
          <button className="btn props-run-btn" onClick={() => onUpdate({ frozen: !node.data?.frozen })}
            title="Frozen nodes keep their cache through 'clear cache' and clean operations"
            style={{ marginTop: 4, fontSize: 11 }}>
            {node.data?.frozen ? '🔓 Unfreeze Cache' : '🔒 Freeze Cache'}
          </button>
          {cacheMsg && (
            <div style={{ marginTop: 4, fontSize: 11, color: '#a0aec0' }}>{cacheMsg}</div>
          )}
        </div>
      )}

      {filePicker && (
        <FilePickerModal
          onSelect={(filePath) => {
            handleInputChange(filePicker, filePath)
            setFilePicker(null)
          }}
          onClose={() => setFilePicker(null)}
        />
      )}
    </>
  )
}
