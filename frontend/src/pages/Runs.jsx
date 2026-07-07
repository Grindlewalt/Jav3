import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Md from '../Md.jsx'

// Watch and walk agent-job trees — including research Jarvis deploys from a
// chat. The list polls so running jobs appear; opening one streams it live
// (snapshot of what already happened, then follows to completion).
const STATUS_TAG = {
  planning: 'planning', delegating: 'planning', running: 'running',
  summarizing: 'running', done: 'done', error: 'error',
}

export default function Runs() {
  const [runs, setRuns] = useState([])
  const [selected, setSelected] = useState(null)
  const [nodes, setNodes] = useState({})
  const [order, setOrder] = useState([])
  const [open, setOpen] = useState({})
  const [live, setLive] = useState(false)
  const [doc, setDoc] = useState(null)
  const [showDoc, setShowDoc] = useState(true)
  const esRef = useRef(null)

  const refresh = () => api('/api/runs').then((r) => setRuns(r.runs))
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)  // pick up newly-deployed runs
    return () => { clearInterval(t); esRef.current?.close() }
  }, [])

  function loadDoc(cid) {
    api(`/api/runs/${cid}/doc`).then((r) => setDoc(r.content ? r : null)).catch(() => setDoc(null))
  }

  function openRun(cid) {
    esRef.current?.close()
    setSelected(cid); setNodes({}); setOrder([]); setOpen({}); setLive(true); setDoc(null)
    loadDoc(cid)
    const es = new EventSource(`/api/runs/${cid}/stream`)
    esRef.current = es
    const up = (id, patch) => setNodes((n) => ({ ...n, [id]: { ...(n[id] || {}), ...patch } }))
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data)
      if (ev.type === 'node_spawned') {
        up(ev.node_id, { id: ev.node_id, parent: ev.parent_id, kind: ev.kind,
                         title: ev.title, depth: ev.depth,
                         status: ev.node_id in nodes ? undefined : 'planning' })
        setOrder((o) => o.includes(ev.node_id) ? o : [...o, ev.node_id])
      }
      if (ev.type === 'node_status') up(ev.node_id, { status: ev.status })
      if (ev.type === 'tool') up(ev.node_id, { tool: ev.name })
      if (ev.type === 'node_done') up(ev.node_id, { status: 'done', rollup: ev.rollup, tool: null })
      if (ev.type === 'error') up(ev.node_id, { status: 'error', tool: ev.message })
      if (ev.type === 'job_final') { setLive(false); es.close(); refresh(); loadDoc(cid) }
    }
    es.onerror = () => { setLive(false); es.close() }
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Runs</div>
        <ul className="file-list">
          {runs.map((r) => (
            <li key={r.id} className={selected === r.id ? 'active' : ''}
                onClick={() => openRun(r.id)}>
              {r.running && <span className="run-dot running" />}
              <span className="grow ellipsis">{r.summary?.replace(/^\[head\]\s*/, '') || `#${r.id}`}</span>
              {r.project_slug && <span className="tag">{r.project_slug}</span>}
            </li>
          ))}
          {runs.length === 0 && <li className="dim">no runs yet — start one from a Research panel, or ask Jarvis to research something</li>}
        </ul>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">pick a run to watch or walk its tree</div>
        ) : (
          <>
            <div className="row" style={{ alignItems: 'center' }}>
              {live && <span className="dim small grow">● live</span>}
              {doc && <button className="ghost" onClick={() => setShowDoc((s) => !s)}>
                {showDoc ? 'show tree' : 'show document'}</button>}
            </div>
            {doc && showDoc ? (
              <div className="run-doc">
                <div className="dim small">{doc.staged ? 'staged (pending approval)' : 'approved'} · {doc.path}</div>
                <Md text={doc.content} />
              </div>
            ) : (
            <div className="run-tree" style={{ padding: '4px 2px' }}>
              {order.map((id) => {
                const n = nodes[id]; if (!n) return null
                return (
                  <div key={id} className="run-node" style={{ marginLeft: (n.depth || 0) * 18 }}>
                    <div className="run-row"
                         onClick={() => n.rollup && setOpen((o) => ({ ...o, [id]: !o[id] }))}>
                      <span className={`tag ${STATUS_TAG[n.status] || (n.rollup ? 'done' : 'planning')}`}>{n.kind}</span>
                      <span className="grow ellipsis">{n.title}</span>
                      {n.tool && <span className="run-activity">⚙ {n.tool}</span>}
                      <span className={`run-dot ${STATUS_TAG[n.status] || (n.rollup ? 'done' : 'planning')}`} />
                      {n.rollup && <span className="dim">{open[id] ? '▾' : '▸'}</span>}
                    </div>
                    {open[id] && n.rollup && <div className="run-rollup"><Md text={n.rollup} /></div>}
                  </div>
                )
              })}
            </div>
            )}
          </>
        )}
      </main>
    </div>
  )
}
