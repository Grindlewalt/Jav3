import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api.js'

export default function ProjectDetail() {
  const { slug } = useParams()
  const [project, setProject] = useState(null)
  const [md, setMd] = useState('')
  const [saved, setSaved] = useState(true)

  useEffect(() => {
    api(`/api/projects/${slug}`).then((p) => { setProject(p); setMd(p.project_md) })
  }, [slug])

  async function save() {
    await api(`/api/projects/${slug}/md`, {
      method: 'PUT',
      body: JSON.stringify({ content: md }),
    })
    setSaved(true)
  }

  if (!project) return <div className="center">…</div>
  return (
    <div className="page">
      <h2>{project.name} {project.loaded && <span className="badge">in context</span>}</h2>
      <p className="dim">project.md — Jarvis reads this when the project is loaded.
        The Summary section feeds all-projects.md.</p>
      <textarea className="md-editor" value={md}
                onChange={(e) => { setMd(e.target.value); setSaved(false) }} rows={24} />
      <button onClick={save} disabled={saved}>{saved ? 'Saved' : 'Save'}</button>
    </div>
  )
}
