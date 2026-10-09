import logo from '../assets/klogo.png'

const PANEL_TOGGLES = [
  { key: 'left', label: 'Components' },
  { key: 'right', label: 'Properties' },
  { key: 'bottom', label: 'Terminal' },
]

export default function Toolbar({ isRunning, wsConnected, selectedNode, onRun, onRunDebug, onStop, onRunSelected, onReset, onClearCache, panelVisible, onTogglePanel }) {
  return (
    <div className="toolbar">
      <img src={logo} alt="kpipeliner" className="app-logo" />

      <button className="btn btn-primary" onClick={onRun} disabled={isRunning}>
        ▶ Run All
      </button>

      {selectedNode && !isRunning && (
        <button className="btn btn-primary" onClick={onRunSelected}>
          ▶ Run Node
        </button>
      )}

      <button className="btn btn-debug" onClick={onRunDebug} disabled={isRunning}>
        ⚙ Debug
      </button>

      <div className="toolbar-sep" />

      <button className="btn btn-danger" onClick={onStop} disabled={!isRunning}>
        ■ Stop
      </button>

      <button
        className="btn btn-reset"
        onClick={onReset}
        disabled={isRunning}
        title="Reset all node states to default"
      >
        ↺ Reset
      </button>

      <button
        className="btn btn-cache"
        onClick={onClearCache}
        disabled={isRunning}
        title="Clear cache for non-frozen nodes"
      >
        ⊗ Clear Cache
      </button>

      <div className="toolbar-right">
        <div className="panel-toggle-group">
          {PANEL_TOGGLES.map(p => (
            <button
              key={p.key}
              className={`btn-panel-toggle ${panelVisible?.[p.key] ? 'active' : ''}`}
              onClick={() => onTogglePanel(p.key)}
              title={`${panelVisible?.[p.key] ? 'Hide' : 'Show'} ${p.label} panel`}
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="toolbar-status">
          <span className={`dot ${isRunning ? 'running' : ''}`} />
          {isRunning ? 'Running...' : 'Idle'}
          {!wsConnected && ' · WS disconnected'}
        </div>
      </div>
    </div>
  )
}
