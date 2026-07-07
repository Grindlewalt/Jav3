import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { api } from '../api.js'
import Md from '../Md.jsx'

// All background work in one place: agent runs, scheduled runs, and research
// heads. The list polls so fresh jobs appear; opening an agent/scheduled job
// shows its transcript, opening a research head shows its document (watch it
// live on the Runs tab).
const KINDS = ['', 'agent', 'scheduled', 'head']
const KIND_LABEL = { agent: 'agent', scheduled: 'scheduled', head: 'research' }

export default function Jobs() {
  const [jobs, setJobs] = useState([])
  const [kind, setKind] = useState('')
  const [selected, setSelected] = useState(null) // the selected job row
  const [messages, setMessages] = useState([])
  const [doc, setDoc] = useState(null)

  const refresh = () =>
    api(`/api/jobs${kind ? `?kind=${kind}` : ''}`).then((r) => setJobs(r.jobs)).catch(() => {})
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000) // pick up newly-launched jobs
    return () => clearInterval(t)
  }, [kind]) // eslint-disable-line react-hooks/exhaustive-deps

  function openJob(j) {
    setSelected(j); setMessages([]); setDoc(null)
    if (j.kind === 'head') {
      api(`/api/runs/${j.id}/doc`).then((r) => setDoc(r.content ? r : null)).catch(() => setDoc(null))
    } else {
      api(`/api/conversations/${j.id}/messages`)
        .then((r) => setMessages(r.messages)).catch(() => setMessages([]))
    }
  }

  async function del(e, j) {
    e.stopPropagation()
    if (!window.confirm(`delete job "${j.summary || `#${j.id}`}"?`)) return
    await api(`/api/conversations/${j.id}`, { method: 'DELETE' })
    if (selected?.id === j.id) { setSelected(null); setMessages([]); setDoc(null) }
    refresh()
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Jobs</div>
        <div className="row">
          <select className="grow" value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map((k) => (
              <option key={k || 'all'} value={k}>{k ? KIND_LABEL[k] : 'all kinds'}</option>
            ))}
          </select>
        </div>
        <ul className="file-list">
          {jobs.map((j) => (
            <li key={j.id} className={selected?.id === j.id ? 'active' : ''}
                onClick={() => openJob(j)}>
              {!j.done && <span className="run-dot running" />}
              <span className="tag">{KIND_LABEL[j.kind] || j.kind}</span>
              <span className="grow ellipsis" title={j.summary || `#${j.id}`}>
                {j.summary || `#${j.id}`}</span>
              {j.project && <span className="tag">{j.project}</span>}
              <button className="win-btn" title="delete job"
                      onClick={(e) => del(e, j)}>×</button>
            </li>
          ))}
          {jobs.length === 0 && <li className="dim">no jobs yet — run an agent,
            set a schedule, or start a research</li>}
        </ul>
        <p className="dim small">agent and scheduled runs show their transcript;
          research jobs show their document (follow them live on the Runs tab).</p>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">pick a job to read its transcript or document</div>
        ) : selected.kind === 'head' ? (
          <>
            <div className="row" style={{ alignItems: 'center' }}>
              <span className="dim small grow">
                {selected.done ? 'finished' : '● running'} · {selected.started_at}</span>
              <NavLink to="/runs" className="ghost-link">watch on Runs</NavLink>
            </div>
            {doc ? (
              <div className="run-doc">
                <div className="dim small">
                  {doc.staged ? 'staged (pending approval)' : 'approved'} · {doc.path}</div>
                <Md text={doc.content} />
              </div>
            ) : (
              <div className="dim center-pad">
                {selected.done ? 'no document found for this run' : 'still running — no document yet'}
              </div>
            )}
          </>
        ) : (
          <>
            <div className="row" style={{ alignItems: 'center' }}>
              <span className="dim small grow">
                {selected.done ? 'finished' : '● running'} · {selected.started_at}</span>
            </div>
            <div className="messages">
              {messages.map((m) => (
                <div key={m.id} className={`msg ${m.role}`}>
                  {m.role === 'assistant'
                    ? <div className="bubble"><Md text={m.content} /></div>
                    : <pre>{m.content}</pre>}
                </div>
              ))}
              {messages.length === 0 && <div className="dim center-pad">no transcript yet</div>}
            </div>
          </>
        )}
      </main>
    </div>
  )
}
