import { useEffect, useRef, useState } from 'react'
import axios from 'axios'

// Pipeline settings (graph_config.yaml's top-level keys), shown in the Properties
// panel when nothing is selected. Every field saves on its own (on blur / Enter /
// toggle) through POST /api/pipeline-settings, which checks the value first.
// `frozen_nodes` is the exception: the graph save rebuilds that list from each node's
// `frozen` flag, so it is edited through those flags (onSetFrozen), never written here.

const LOG_LEVELS = ['DEBUG', 'INFO', 'OUTPUT', 'WARNING', 'ERROR']
// Changes that alter which node types exist / are offered → reload the palette.
const PALETTE_KEYS = ['available_nodes', 'load_default_base_nodes_dir', 'custom_base_nodes_dirs']

function TextSetting({ label, value, onCommit, placeholder, multiline, hint }) {
  const [val, setVal] = useState(value)
  useEffect(() => setVal(value), [value])
  const commit = () => { if (val !== value) onCommit(val) }
  return (
    <div className="props-field">
      <label>{label}</label>
      {multiline
        ? <textarea rows={3} value={val} placeholder={placeholder}
            onChange={e => setVal(e.target.value)} onBlur={commit} />
        : <input value={val} placeholder={placeholder}
            onChange={e => setVal(e.target.value)} onBlur={commit}
            onKeyDown={e => e.key === 'Enter' && e.target.blur()} />}
      {hint && <div className="pipeline-hint">{hint}</div>}
    </div>
  )
}

function CheckSetting({ label, checked, onChange, hint }) {
  return (
    <div className="props-field">
      <label className="pipeline-check">
        <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
        {label}
      </label>
      {hint && <div className="pipeline-hint">{hint}</div>}
    </div>
  )
}

// A list of paths: edit in place (saved on blur), ✕ to remove, + Add for a new one.
function DirList({ dirs, onChange }) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const d = draft.trim()
    if (d && !dirs.includes(d)) onChange([...dirs, d])
    setDraft('')
  }
  return (
    <>
      {dirs.length === 0 && <div className="pipeline-hint">None.</div>}
      {dirs.map((d, i) => (
        <DirRow key={`${i}-${d}`} value={d}
          onCommit={v => onChange(dirs.map((x, j) => (j === i ? v : x)).filter(Boolean))}
          onRemove={() => onChange(dirs.filter((_, j) => j !== i))} />
      ))}
      <div className="pipeline-row">
        <input value={draft} placeholder="path (absolute or relative to the pipeline)"
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && add()} />
        <button className="btn pipeline-small-btn" onClick={add} disabled={!draft.trim()}>+ Add</button>
      </div>
    </>
  )
}

function DirRow({ value, onCommit, onRemove }) {
  const [val, setVal] = useState(value)
  useEffect(() => setVal(value), [value])
  return (
    <div className="pipeline-row">
      <input value={val} onChange={e => setVal(e.target.value)}
        onBlur={() => val.trim() !== value && onCommit(val.trim())}
        onKeyDown={e => e.key === 'Enter' && e.target.blur()} />
      <button className="btn pipeline-small-btn" title={`Remove ${value}`} onClick={onRemove}>✕</button>
    </div>
  )
}

// Checkbox list (node types / node names).
function PickList({ options, selected, onChange, empty }) {
  const sel = new Set(selected)
  if (options.length === 0) return <div className="pipeline-hint">{empty}</div>
  return (
    <div className="pipeline-picklist">
      {options.map(o => (
        <label key={o} className="pipeline-check">
          <input type="checkbox" checked={sel.has(o)}
            onChange={e => onChange(e.target.checked ? [...selected, o] : selected.filter(x => x !== o))} />
          {o}
        </label>
      ))}
    </div>
  )
}

export default function PipelineProperties({ nodeIds, frozenIds, onSetFrozen, onPaletteChange }) {
  const [data, setData] = useState(null)      // { settings, node_types, missing_types }
  const [loadErr, setLoadErr] = useState(null)
  const [msg, setMsg] = useState(null)        // { kind: 'ok' | 'error', text }
  const msgTimer = useRef(null)

  const load = () =>
    axios.get('/api/pipeline-settings')
      .then(r => (r.data?.ok ? setData(r.data) : setLoadErr(r.data?.msg || 'No pipeline settings.')))
      .catch(() => setLoadErr('Could not load the pipeline settings.'))

  useEffect(() => {
    load()
    return () => clearTimeout(msgTimer.current)
  }, [])

  const flash = (kind, text) => {
    clearTimeout(msgTimer.current)
    setMsg({ kind, text })
    if (kind === 'ok') msgTimer.current = setTimeout(() => setMsg(null), 2500)
  }

  const save = changes => {
    // Show the change at once (a checkbox must flip on click, not when the server
    // answers); the server's reply then replaces it with what was really saved.
    setData(d => (d ? { ...d, settings: { ...d.settings, ...changes } } : d))
    axios.post('/api/pipeline-settings', { changes })
      .then(r => {
        setData(r.data)
        flash('ok', 'Saved to graph_config.yaml')
        if (Object.keys(changes).some(k => PALETTE_KEYS.includes(k))) onPaletteChange?.()
      })
      .catch(err => {
        flash('error', err?.response?.data?.msg || 'Saving failed.')
        load()   // nothing was saved: the fields go back to the file's values
      })
  }

  if (loadErr) {
    return (
      <>
        <div className="panel-header">Properties</div>
        <div className="props-empty">{loadErr}</div>
      </>
    )
  }
  if (!data) {
    return (
      <>
        <div className="panel-header">Properties</div>
        <div className="props-empty">Loading pipeline settings…</div>
      </>
    )
  }

  const s = data.settings
  // types listed in available_nodes but not installed stay visible so they can be unticked
  const typeOptions = [...new Set([...(data.node_types || []), ...s.available_nodes])].sort()

  return (
    <>
      <div className="panel-header">Properties</div>
      <div className="props-node-title">
        Pipeline
        <div className="props-node-type">graph_config.yaml — select a node to see its properties</div>
      </div>

      {msg && <div className={`pipeline-msg pipeline-msg-${msg.kind}`}>{msg.text}</div>}
      {data.missing_types?.length > 0 && (
        <div className="pipeline-msg pipeline-msg-warn">
          The graph uses node type(s) the base-node dirs below don't provide:{' '}
          <b>{data.missing_types.join(', ')}</b>. Reloading the page will ask what to do with those nodes.
        </div>
      )}

      <div className="props-section">
        <div className="props-section-title">General</div>
        <TextSetting label="Name" value={s.name} onCommit={v => save({ name: v })} />
        <TextSetting label="Description" value={s.description} multiline onCommit={v => save({ description: v })} />
        <TextSetting label="Author" value={s.author} onCommit={v => save({ author: v })} />
        <TextSetting label="Date" value={s.date} placeholder="YYYY-MM-DD" onCommit={v => save({ date: v })} />
        <TextSetting label="Version" value={s.version} onCommit={v => save({ version: v })} />
      </div>

      <div className="props-section">
        <div className="props-section-title">Node types</div>
        <CheckSetting label="Load the default base-nodes dir" checked={s.load_default_base_nodes_dir}
          onChange={v => save({ load_default_base_nodes_dir: v })}
          hint="default_base_nodes_dir of the installation's config.yaml" />
        <div className="props-field">
          <label>Custom base-nodes dirs</label>
          <DirList dirs={s.custom_base_nodes_dirs} onChange={v => save({ custom_base_nodes_dirs: v })} />
          <div className="pipeline-hint">Scanned before the default dir — on a name clash the custom type wins.</div>
        </div>
        <div className="props-field">
          <label>Available node types</label>
          <PickList options={typeOptions} selected={s.available_nodes}
            onChange={v => save({ available_nodes: v })} empty="No node types found." />
          <div className="pipeline-hint">
            {s.available_nodes.length === 0 ? 'None ticked: every type is offered.' : 'Only the ticked types are offered in the Components panel.'}
          </div>
        </div>
      </div>

      <div className="props-section">
        <div className="props-section-title">Cache &amp; data</div>
        <div className="props-field">
          <label>Frozen nodes (cache is never cleared)</label>
          <PickList options={nodeIds} selected={frozenIds} onChange={onSetFrozen} empty="No nodes in this pipeline." />
        </div>
        <TextSetting label="Cache dir" value={s.cache_dir} onCommit={v => save({ cache_dir: v })}
          hint="Relative to the pipeline dir, or absolute. Existing cache files are not moved." />
        <TextSetting label="Data dir" value={s.data_dir} onCommit={v => save({ data_dir: v })}
          hint="Relative to the pipeline dir, or absolute. Existing files are not moved." />
      </div>

      <div className="props-section">
        <div className="props-section-title">Groups</div>
        <TextSetting label="Groups dir" value={s.groups_dir} placeholder="(installation-wide group dir)"
          onCommit={v => save({ groups_dir: v })}
          hint="Where exported groups are saved and read from first. Empty: the installation-wide group_base_nodes dir." />
      </div>

      <div className="props-section">
        <div className="props-section-title">Logging</div>
        <div className="props-field">
          <label>Log level</label>
          <select value={s.log_level} onChange={e => save({ log_level: e.target.value })}>
            {LOG_LEVELS.map(l => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <CheckSetting label="Write a log file per node" checked={s.log_enabled}
          onChange={v => save({ log_enabled: v })}
          hint="cache/<uuid>/<node>/log/run.log, appended across runs" />
      </div>
    </>
  )
}
