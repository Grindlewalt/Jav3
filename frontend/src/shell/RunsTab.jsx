import { useEffect, useState } from 'react'
import { api } from '../api.js'
import JobTree from '../JobTree.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { ago } from '../format.js'

// The multi-agent jobs around this chat: the ones this conversation launched
// (research, deploy_agents, orchestrate — /messages `jobs`, the durable
// parent link) and, in a project, every other run in that project
// (GET /api/jobs). One row each; a row opens its live tree (JobTree streams
// /api/runs/{id}/stream). The transcript keeps only a one-line mention, so the
// tallest thing in a thread lives here instead.

const POLL_MS = 5000

export default function RunsTab({ project, chatJobs = [] }) {
  const [projectJobs, setProjectJobs] = useState([])
  const [open, setOpen] = useState(null)

  useEffect(() => {
    setProjectJobs([])
    if (!project) return undefined
    const load = () => api('/api/jobs')
      .then((r) => setProjectJobs((r.jobs || []).filter((j) => j.project === project)))
      .catch(() => {})
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [project])

  const seen = new Set(chatJobs.map((j) => j.root_id))
  const rows = [
    ...chatJobs.map((j) => ({ id: j.root_id, title: j.title, at: j.created_at,
                              running: j.running, kind: 'this chat' })),
    ...projectJobs.filter((j) => !seen.has(j.id))
      .map((j) => ({ id: j.id, title: j.summary, at: j.started_at, running: !j.done,
                     kind: j.agent_slug ? `@${j.agent_slug}` : j.kind })),
  ].sort((a, b) => (b.running - a.running) || String(b.at).localeCompare(String(a.at)))

  if (rows.length === 0) {
    return <EmptyState pad>No runs yet. Research, agent teams and plan runs
      {project ? ' in this project' : ' this chat starts'} show up here.</EmptyState>
  }
  return (
    <div className="pane-col">
      <ul className="dock-runs">
        {rows.map((r) => (
          <li key={r.id} className={open === r.id ? 'open' : undefined}>
            <button type="button" className="dock-run" aria-expanded={open === r.id}
                    onClick={() => setOpen(open === r.id ? null : r.id)}>
              <span className={`sh-dot ${r.running ? 'live' : 'ok'}`} aria-hidden="true" />
              <span className="ellipsis grow" title={r.title}>{r.title || `run #${r.id}`}</span>
              <span className="dim small">{r.running ? 'running' : ago(r.at)}</span>
              <span className="dim small">{r.kind}</span>
            </button>
            {open === r.id && <div className="dock-run-tree"><JobTree cid={r.id} /></div>}
          </li>
        ))}
      </ul>
    </div>
  )
}
