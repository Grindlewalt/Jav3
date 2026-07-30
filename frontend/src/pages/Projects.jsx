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
  const [repoUrl, setRepoUrl] = useState('')
  const [creating, setCreating] = useState(false)

  async function refresh() {
    const r = await api('/api/projects')
    setProjects(r.projects)
    setDeleted(r.deleted || [])
    setActive(r.active)
  }
  useEffect(() => { refresh() }, [])

  // One form, both paths: a GitHub URL turns the create into a clone-import,
  // and name/description ride along either way (the import derives a name
  // from the repo when the field is left empty).
  async function create(e) {
    e.preventDefault()
    setError(null)
    setCreating(true)
    const url = repoUrl.trim()
    try {
      if (url) {
        await api('/api/projects/import', {
          method: 'POST',
          body: JSON.stringify({ url, name: name.trim() || undefined,
                                 summary: summary.trim() || undefined }),
        })
      } else {
        await api('/api/projects', {
          method: 'POST',
          body: JSON.stringify({ name, summary: summary || undefined }),
        })
      }
      setName(''); setSummary(''); setRepoUrl('')
      refresh()
    } catch (err) { setError(err.detail) }
    setCreating(false)
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
        <input placeholder={repoUrl.trim()
                 ? 'project name (repo name if empty)' : 'project name'}
               value={name} onChange={(e) => setName(e.target.value)}
               required={!repoUrl.trim()} />
        <input placeholder="what are you building? (one line)" value={summary}
               onChange={(e) => setSummary(e.target.value)} />
        <input className="gh-url" type="url"
               placeholder="https://github.com/owner/repo (optional — clone it in)"
               value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} />
        <button type="submit" disabled={creating}>
          {creating && repoUrl.trim() ? 'cloning…'
            : repoUrl.trim() ? 'Clone & create' : 'Create'}</button>
        {error && <span className="error">{error}</span>}
      </form>
      <ul className="project-list">
        {projects.map((p) => (
          <li key={p.slug}>
            <Link to={`/projects/${p.slug}`}>{p.name}</Link>
            <button className="win-btn" title="rename project"
                    onClick={async (e) => {
                      e.preventDefault()
                      const next = window.prompt('Rename project', p.name)
                      if (next === null || !next.trim()) return
                      try {
                        await api(`/api/projects/${p.slug}/name`, {
                          method: 'PUT',
                          body: JSON.stringify({ name: next.trim() }) })
                        refresh()
                      } catch (err) { setError(err.detail || String(err)) }
                    }}>✎</button>
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
        <details className="deleted-fold">
          <summary>
            Recently deleted ({deleted.length})
            <span className="chev" aria-hidden="true">›</span>
          </summary>
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
        </details>
      )}
    </div>
  )
}
