import { useEffect, useRef, useState } from 'react'
import Md from './Md.jsx'
import { followRun } from './runFeed.js'

// Live agent-job tree, keyed by the job's HEAD conversation id. Snapshot +
// follow through runFeed.js (the shared event stream, never a socket per
// tree); embeddable anywhere — the chat activity area, the Jobs page.
// Extracted from the retired Runs tab.
const STATUS_TAG = {
  planning: 'planning', delegating: 'planning', running: 'running',
  summarizing: 'running', done: 'done', error: 'error',
}

export default function JobTree({ cid, onFinal }) {
  const [nodes, setNodes] = useState({})
  const [order, setOrder] = useState([])
  const [open, setOpen] = useState({})
  const [live, setLive] = useState(true)
  const onFinalRef = useRef(onFinal)
  onFinalRef.current = onFinal

  useEffect(() => {
    setNodes({}); setOrder([]); setOpen({}); setLive(true)
    const up = (id, patch) =>
      setNodes((n) => ({ ...n, [id]: { ...(n[id] || {}), ...patch } }))
    let stop = null
    let ended = false
    const onEvent = (ev) => {
      if (ended) return
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
      // the failure is its own field: it was stored in `tool` and drawn as if
      // the agent were running a tool called by the error's text
      if (ev.type === 'error') up(ev.node_id, { status: 'error', tool: null, err: ev.message })
      if (ev.type === 'job_final') {
        ended = true; setLive(false); stop?.(); onFinalRef.current?.()
      }
    }
    // no connection of its own: the snapshot is a fetch and the live events
    // ride the shared per-browser stream (runFeed.js). Only an unreadable run
    // ends the live view; a stream blip is re-synced there.
    stop = followRun(cid, onEvent, () => setLive(false))
    return () => stop()
  }, [cid])

  // one line that says how the job stands, so a long tree needs no counting
  const all = order.map((id) => nodes[id]).filter(Boolean)
  const failed = all.filter((n) => n.status === 'error').length
  const finished = all.filter((n) => n.status === 'done' || n.rollup).length
  const working = all.length - failed - finished
  return (
    <div className="run-tree" style={{ padding: '4px 2px' }}>
      <div className="run-summary dim small">
        {live ? <span className="run-live">● live</span> : <span>finished</span>}
        {all.length === 0
          ? <span>{live ? ' · waiting for agents…' : ' · no agents recorded'}</span>
          : <span> · {all.length} agent{all.length === 1 ? '' : 's'}
              {live && working > 0 ? ` · ${working} working` : ''}
              {finished > 0 ? ` · ${finished} done` : ''}</span>}
        {failed > 0 && <span className="run-failed"> · {failed} failed</span>}
      </div>
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
              {n.err && <span className="run-err ellipsis" title={n.err}>{n.err}</span>}
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
