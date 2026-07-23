import { useEffect, useRef, useState } from 'react'
import Md from './Md.jsx'

// Live agent-job tree, keyed by the job's HEAD conversation id. Streams
// /api/runs/{cid}/stream (snapshot + follow); embeddable anywhere — the chat
// activity area, the Jobs page. Extracted from the retired Runs tab.
const STATUS_TAG = {
  planning: 'planning', delegating: 'planning', running: 'running',
  summarizing: 'running', done: 'done', error: 'error',
}

export default function JobTree({ cid, onFinal }) {
  const [nodes, setNodes] = useState({})
  const [order, setOrder] = useState([])
  const [open, setOpen] = useState({})
  const [live, setLive] = useState(true)
  const esRef = useRef(null)

  useEffect(() => {
    setNodes({}); setOrder([]); setOpen({}); setLive(true)
    const es = new EventSource(`/api/runs/${cid}/stream`)
    esRef.current = es
    const up = (id, patch) =>
      setNodes((n) => ({ ...n, [id]: { ...(n[id] || {}), ...patch } }))
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data)
      if (ev.type === 'node_spawned') {
        setNodes((n) => ({ ...n, [ev.node_id]: {
          status: 'planning', ...(n[ev.node_id] || {}),
          id: ev.node_id, parent: ev.parent_id, kind: ev.kind,
          title: ev.title, depth: ev.depth } }))
        setOrder((o) => (o.includes(ev.node_id) ? o : [...o, ev.node_id]))
      }
      if (ev.type === 'node_status') up(ev.node_id, { status: ev.status })
      if (ev.type === 'tool') up(ev.node_id, { tool: ev.name })
      if (ev.type === 'node_done') up(ev.node_id, { status: 'done', rollup: ev.rollup, tool: null })
      if (ev.type === 'error') up(ev.node_id, { status: 'error', tool: ev.message })
      if (ev.type === 'job_final') { setLive(false); es.close(); onFinal?.() }
    }
    // transient blips auto-reconnect (the browser retries while CONNECTING);
    // only a dead socket ends the live view
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setLive(false) }
    return () => es.close()
  }, [cid]) // eslint-disable-line

  return (
    <div className="run-tree" style={{ padding: '4px 2px' }}>
      {live && <div className="dim small">● live</div>}
      {order.map((id) => {
        const n = nodes[id]; if (!n) return null
        const tag = STATUS_TAG[n.status] || (n.rollup ? 'done' : 'planning')
        return (
          <div key={id} className="run-node" style={{ marginLeft: (n.depth || 0) * 18 }}>
            <div className="run-row"
                 onClick={() => n.rollup && setOpen((o) => ({ ...o, [id]: !o[id] }))}>
              <span className={`tag ${tag}`}>{n.kind}</span>
              <span className="grow ellipsis">{n.title}</span>
              {n.tool && <span className="run-activity">⚙ {n.tool}</span>}
              <span className={`run-dot ${tag}`} />
              {n.rollup && <span className="dim">{open[id] ? '▾' : '▸'}</span>}
            </div>
            {open[id] && n.rollup && <div className="run-rollup"><Md text={n.rollup} /></div>}
          </div>
        )
      })}
    </div>
  )
}
