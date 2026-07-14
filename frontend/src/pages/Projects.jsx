import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'

export default function Projects() {
  const [projects, setProjects] = useState([])
  const [deleted, setDeleted] = useState([])
  const [active, setActive] = useState(null)
  const [name, setName] = useState('')
  const [summary, setSummary] = useState('')
  const [error, setError] = useState(null)

  async function refresh() {
    const r = await api('/api/projects')
    setProjects(r.projects)
    setDeleted(r.deleted || [])
    setActive(r.active)
  }
  useEffect(() => { refresh() }, [])

  async function create(e) {
    e.preventDefault()
    setError(null)
    try {
      await api('/api/projects', {
        method: 'POST',
        body: JSON.stringify({ name, summary: summary || undefined }),
      })
      setName(''); setSummary('')
      refresh()
    } catch (err) { setError(err.detail) }
  }

  async function load(slug) {
    await api(`/api/projects/${slug}/load`, { method: 'POST' })
    refresh()
  }
  async function unload() {
    await api('/api/projects/unload', { method: 'POST' })
    refresh()
  }
  async function softDelete(slug) {
    if (!window.confirm(`move "${slug}" to recently deleted?`)) return
    await api(`/api/projects/${slug}`, { method: 'DELETE' })
    refresh()
  }
  async function restore(slug) {
    await api(`/api/projects/${slug}/restore`, { method: 'POST' })
    refresh()
  }
  async function purge(slug) {
    if (!window.confirm(`permanently delete "${slug}" and all its files? This cannot be undone.`)) return
    await api(`/api/projects/${slug}/purge`, { method: 'DELETE' })
    refresh()
  }
  async function setAutonomy(slug, level) {
    await api(`/api/projects/${slug}/autonomy`, {
      method: 'PUT', body: JSON.stringify({ level }),
    })
    refresh()
  }
  return (
    <div className="page">
      <h2>Projects</h2>
      <form className="create-project" onSubmit={create}>
        <input placeholder="project name" value={name}
               onChange={(e) => setName(e.target.value)} required />
        <input placeholder="what are you building? (one line)" value={summary}
               onChange={(e) => setSummary(e.target.value)} />
        <button type="submit">Create</button>
        {error && <span className="error">{error}</span>}
      </form>
      <ul className="project-list">
        {projects.map((p) => (
          <li key={p.slug}>
            <Link to={`/projects/${p.slug}`}>{p.name}</Link>
            <code>{p.slug}</code>
            {active === p.slug
              ? <button onClick={unload}>Unload from context</button>
              : <button onClick={() => load(p.slug)}>Load into context</button>}
            {active === p.slug && <span className="badge">in context</span>}
            <select className="autonomy-sel" value={p.autonomy || 'full'}
                    title="how much the agent may do unattended in this project"
                    onChange={(e) => setAutonomy(p.slug, e.target.value)}>
              <option value="read_only">read-only</option>
              <option value="stage">stage edits</option>
              <option value="gated">agents + research</option>
              <option value="full">full (commit)</option>
            </select>
            <button className="ghost danger" onClick={() => softDelete(p.slug)}>delete</button>
          </li>
        ))}
        {projects.length === 0 && <li className="dim">no projects yet</li>}
      </ul>

      {deleted.length > 0 && (
        <>
          <h3 className="section-h">Recently deleted</h3>
          <ul className="project-list">
            {deleted.map((p) => (
              <li key={p.slug} className="deleted">
                <span>{p.name}</span>
                <code>{p.slug} · deleted {p.deleted_at?.slice(0, 16)}</code>
                <button className="ghost" onClick={() => restore(p.slug)}>restore</button>
                <button className="ghost danger" onClick={() => purge(p.slug)}>delete forever</button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
