import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Md from '../Md.jsx'

// Everything Jarvis made in project-less chats, grouped by chat: view/edit,
// turn a store into a real project, or merge its files into an existing one.
export default function Artifacts() {
  const [artifacts, setArtifacts] = useState([])
  const [projects, setProjects] = useState([])
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(null)          // { slug, path }
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [preview, setPreview] = useState(true)

  const refresh = (query = q) =>
    api(`/api/artifacts${query ? `?q=${encodeURIComponent(query)}` : ''}`)
      .then((r) => setArtifacts(r.artifacts))
  useEffect(() => {
    refresh('')
    api('/api/projects').then((r) => setProjects(r.projects))
  }, [])

  useEffect(() => {
    if (!sel) return
    api(`/api/projects/${sel.slug}/file?path=${encodeURIComponent(sel.path)}`)
      .then((r) => { setContent(r.binary ? '(binary file)' : r.content); setDirty(false) })
      .catch(() => setContent(''))
  }, [sel])

  async function save() {
    await api(`/api/projects/${sel.slug}/file`, {
      method: 'PUT', body: JSON.stringify({ path: sel.path, content }) })
    setDirty(false)
  }

  async function convert(a) {
    const name = window.prompt('project name for this artifact store', a.title)
    if (!name) return
    await api(`/api/artifacts/${a.slug}/convert`, {
      method: 'POST', body: JSON.stringify({ name }) })
    window.alert(`now a project: ${name}`)
    refresh()
  }

  async function merge(a, target) {
    if (!target) return
    const r = await api(`/api/artifacts/${a.slug}/merge`, {
      method: 'POST', body: JSON.stringify({ target }) })
    window.alert(`merged into ${target}: ${r.merged.join(', ')}`)
  }

  async function del(a) {
    if (!window.confirm(`delete artifact store from "${a.title}" (${a.files.length} files)?`)) return
    await api(`/api/artifacts/${a.slug}`, { method: 'DELETE' })
    if (sel?.slug === a.slug) setSel(null)
    refresh()
  }

  const isMd = sel && /\.md$/i.test(sel.path)
  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Artifacts</div>
        <input placeholder="search name or content…" value={q}
               onChange={(e) => { setQ(e.target.value); refresh(e.target.value) }} />
        {artifacts.length === 0 && (
          <p className="dim small">nothing yet — files Jarvis creates in a chat
            with no project loaded land here</p>
        )}
        {artifacts.map((a) => (
          <div key={a.slug} className="artifact-group">
            <div className="side-title row">
              <span className="grow ellipsis" title={a.title}>{a.title}</span>
              <button className="win-btn" title="delete store" onClick={() => del(a)}>×</button>
            </div>
            <ul className="file-list">
              {a.files.map((f) => (
                <li key={f.path}
                    className={sel?.slug === a.slug && sel?.path === f.path ? 'active' : ''}
                    onClick={() => setSel({ slug: a.slug, path: f.path })}>
                  <span className="grow ellipsis">{f.path}</span>
                  <span className="dim small">{f.size}B</span>
                </li>
              ))}
            </ul>
            <div className="row">
              <button className="ghost" onClick={() => convert(a)}>→ project</button>
              <select defaultValue="" onChange={(e) => { merge(a, e.target.value); e.target.value = '' }}>
                <option value="" disabled>merge into…</option>
                {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
              </select>
            </div>
          </div>
        ))}
      </aside>
      <main className="editor-pane">
        {!sel ? (
          <p className="dim center-pad">pick a file to view or edit</p>
        ) : (
          <>
            <div className="pane-head">
              <h3>{sel.path}</h3>
              {isMd && (
                <button className="ghost" onClick={() => setPreview((v) => !v)}>
                  {preview ? '✎ edit' : '👁 preview'}</button>
              )}
              <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            {isMd && preview
              ? <div className="md-preview grow"><Md text={content} /></div>
              : <textarea className="md-editor grow" value={content} spellCheck={false}
                          onChange={(e) => { setContent(e.target.value); setDirty(true) }} />}
          </>
        )}
      </main>
    </div>
  )
}
