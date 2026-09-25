import { useState } from 'react'
import { chatStream } from '../api.js'
import { notifyError } from '../notify.js'
import Md from '../Md.jsx'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// Watch the funnel work: a topic decomposes into a tree of bots (head ->
// leaders -> subagents) that light up live. Each node shows its status, current
// tool activity, and an expandable rollup (cascading fidelity: collapsed by
// default). Built purely from the streamed node_spawned parent/child links.
const STATUS_TAG = {
  planning: 'planning', delegating: 'planning', running: 'running',
  summarizing: 'running', done: 'done', error: 'error',
}
export default function ResearchPanel({ slug, state, setState }) {
  const [nodes, setNodes] = useState({})   // id -> {id,parent,kind,title,depth,status,tool,rollup}
  const [order, setOrder] = useState([])   // node ids in spawn order
  const [open, setOpen] = useState({})      // id -> rollup expanded
  const [busy, setBusy] = useState(false)
  const [doc, setDoc] = useState(null)
  const [peakAsk, setPeakAsk] = useState(null)   // in-page; iOS eats confirm()
  const topic = state.topic || ''
  const angles = state.angles || 4

  const upNode = (id, patch) =>
    setNodes((n) => ({ ...n, [id]: { ...(n[id] || {}), ...patch } }))

  async function run(confirmPeak = false) {
    if (!topic.trim() || busy) return
    setBusy(true); setNodes({}); setOrder([]); setOpen({}); setDoc(null)
    try {
      await chatStream({ topic, angles: Number(angles) || 4, confirm_peak: confirmPeak,
                         project: slug }, (ev) => {
        if (ev.type === 'node_spawned') {
          upNode(ev.node_id, { id: ev.node_id, parent: ev.parent_id, kind: ev.kind,
                               title: ev.title, depth: ev.depth, status: 'planning' })
          setOrder((o) => o.includes(ev.node_id) ? o : [...o, ev.node_id])
        }
        if (ev.type === 'node_status') upNode(ev.node_id, { status: ev.status })
        if (ev.type === 'tool') upNode(ev.node_id, { tool: ev.name })
        if (ev.type === 'node_done') upNode(ev.node_id, { status: 'done', rollup: ev.rollup, tool: null })
        if (ev.type === 'error') upNode(ev.node_id, { status: 'error', tool: ev.message })
        if (ev.type === 'job_final') setDoc({ path: ev.doc_path, usage: ev.usage })
      }, '/api/runs/research')
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) {
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        setPeakAsk(true)
      } else notifyError(err)
    }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      {peakAsk && (
        <div className="peak-ask compact" role="alertdialog"
             aria-label="peak pricing confirmation">
          <span className="grow">Peak pricing right now — this research costs 2×.</span>
          <button type="button" className="ghost"
                  onClick={() => setPeakAsk(null)}>Cancel</button>
          <button type="button"
                  onClick={() => { setPeakAsk(null); run(true) }}>Research anyway</button>
        </div>
      )}
      <form className="row" onSubmit={(e) => { e.preventDefault(); run() }}>
        <input className="grow" placeholder="research topic…" value={topic}
               onChange={(e) => setState({ topic: e.target.value })} />
        <input type="number" min="2" max="6" value={angles} style={{ width: '3.5em' }}
               title="angles" onChange={(e) => setState({ angles: e.target.value })} />
        <button type="submit" disabled={busy || !topic.trim()}>{busy ? '…' : 'Research'}</button>
      </form>
      <div className="run-tree">
        {order.length === 0 && <EmptyState pad>
          give a topic and watch the bots divide it up</EmptyState>}
        {order.map((id) => {
          const n = nodes[id]; if (!n) return null
          return (
            <div key={id} className="run-node" style={{ marginLeft: (n.depth || 0) * 16 }}>
              <div className="run-row" onClick={() => n.rollup && setOpen((o) => ({ ...o, [id]: !o[id] }))}>
                <span className={`tag ${STATUS_TAG[n.status] || 'planning'}`}>{n.kind}</span>
                <span className="grow ellipsis">{n.title}</span>
                {n.tool && <span className="run-activity">⚙ {n.tool}</span>}
                <span className={`run-dot ${STATUS_TAG[n.status] || 'planning'}`} />
                {n.rollup && <span className="dim">{open[id] ? '▾' : '▸'}</span>}
              </div>
              {open[id] && n.rollup && <div className="run-rollup"><Md text={n.rollup} /></div>}
            </div>
          )
        })}
      </div>
      {doc && <div className="dim small">document written to <code>{doc.path}</code>
        {doc.usage && <> · {doc.usage}</>}</div>}
    </div>
  )
}
