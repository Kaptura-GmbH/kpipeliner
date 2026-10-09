import { useEffect, useRef } from 'react'

export default function TerminalPanel({ logs, progress, systemStats, isRunning, pendingCount, runningCount }) {
  const logRef = useRef(null)

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [logs])

  const activeProgress = Object.entries(progress).filter(([, p]) => p.status === 'RUNNING')

  return (
    <div className="terminal-panel">
      <div className="terminal-header">
        <span className="terminal-stat">
          Queue: <span>{pendingCount}</span>
        </span>
        <span className="terminal-stat">
          Running: <span>{runningCount}</span>
        </span>
        <span className="terminal-stat">
          CPU:
          <span className="cpu-bar">
            <div className="cpu-bar-fill" style={{ width: `${systemStats.cpu}%` }} />
          </span>
          {' '}{Math.round(systemStats.cpu)}%
        </span>
        {systemStats.gpu !== null && (
          <span className="terminal-stat">
            GPU:
            <span className="gpu-bar">
              <div className="gpu-bar-fill" style={{ width: `${systemStats.gpu}%` }} />
            </span>
            {' '}{Math.round(systemStats.gpu)}%
          </span>
        )}
        <span className="terminal-stat">
          MEM: <span>{Math.round(systemStats.mem)}%</span>
        </span>
        {isRunning && <span style={{ color: '#f6ad55', fontSize: 11 }}>● running</span>}
      </div>

      {activeProgress.length > 0 && (
        <div className="progress-list">
          {activeProgress.map(([name, p]) => (
            <div key={name} className="progress-item">
              <span className="pi-name">{name}</span>
              <div className="progress-bar-wrap">
                <div
                  className="progress-bar-fill"
                  style={{ width: `${p.max > 0 ? (p.progress / p.max) * 100 : 0}%` }}
                />
              </div>
              <span style={{ fontSize: 10, color: '#a0aec0' }}>
                {p.progress}/{p.max}
              </span>
              {p.message && (
                <span style={{ fontSize: 10, color: '#718096', maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {p.message}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="terminal-log" ref={logRef}>
        {logs.map((l, i) => (
          <div key={i} className={`log-line ${l.cls || 'log-info'}`}>
            {l.text}
          </div>
        ))}
        {logs.length === 0 && (
          <div style={{ color: '#4a5568', fontSize: 11 }}>Terminal output will appear here when running nodes.</div>
        )}
      </div>
    </div>
  )
}
