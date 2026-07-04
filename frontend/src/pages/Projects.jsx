import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'

export default function Projects() {
  const [projects, setProjects] = useState([])
  const [active, setActive] = useState(null)
  const [name, setName] = useState('')
  const [summary, setSummary] = useState('')
  const [error, setError] = useState(null)

  async function refresh() {
    const r = await api('/api/projects')
    setProjects(r.projects)
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
          </li>
        ))}
        {projects.length === 0 && <li className="dim">no projects yet</li>}
      </ul>
    </div>
  )
}
