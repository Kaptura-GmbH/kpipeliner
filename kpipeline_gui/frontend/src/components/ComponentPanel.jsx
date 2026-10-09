import { useState, useMemo } from 'react'

// Exported group components ('groups' / 'groups/<cat>') list after all
// base-node categories.
export function compareCategories(a, b) {
  const ga = a === 'groups' || a.startsWith('groups/')
  const gb = b === 'groups' || b.startsWith('groups/')
  if (ga !== gb) return ga ? 1 : -1
  return a.localeCompare(b)
}

export default function ComponentPanel({ nodeTypes, onAddNode }) {
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState({})

  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return nodeTypes.filter(n =>
      !q || n.type.toLowerCase().includes(q) || n.description?.toLowerCase().includes(q)
    )
  }, [nodeTypes, search])

  const byCategory = useMemo(() => {
    const map = {}
    filtered.forEach(n => {
      const cat = n.category || 'General'
      if (!map[cat]) map[cat] = []
      map[cat].push(n)
    })
    return map
  }, [filtered])

  function handleDragStart(e, type) {
    e.dataTransfer.setData('application/kpipeliner-node-type', type)
    e.dataTransfer.effectAllowed = 'copy'
  }

  return (
    <>
      <div className="panel-header">Components</div>
      <input
        className="comp-search"
        placeholder="Search nodes..."
        value={search}
        onChange={e => setSearch(e.target.value)}
      />
      {Object.entries(byCategory).sort(([a], [b]) => compareCategories(a, b)).map(([cat, nodes]) => (
        <div key={cat} className="comp-category">
          <div
            className="comp-category-header"
            onClick={() => setCollapsed(prev => ({ ...prev, [cat]: !prev[cat] }))}
          >
            <span>{collapsed[cat] ? '▶' : '▼'}</span>
            {cat}
            <span style={{ marginLeft: 'auto', color: '#4a5568' }}>{nodes.length}</span>
          </div>
          {!collapsed[cat] && nodes.map(n => (
            <div
              key={n.type}
              className="comp-node-item"
              title={n.description}
              draggable
              onDragStart={e => handleDragStart(e, n.type)}
              onClick={() => onAddNode(n.type)}
            >
              <span className="node-icon">{n.is_group ? '⊞' : '◈'}</span>
              {n.type}
              {n.kwui && (
                <span className="comp-kwui-badge" title="kwui-enabled: builds a web UI via self.ui in a `kpl run --kwui` run">
                  UI
                </span>
              )}
            </div>
          ))}
        </div>
      ))}
      {Object.keys(byCategory).length === 0 && (
        <div style={{ padding: '16px', fontSize: '12px', color: '#718096' }}>
          {nodeTypes.length === 0 ? 'No node types installed.' : 'No results.'}
        </div>
      )}
    </>
  )
}
