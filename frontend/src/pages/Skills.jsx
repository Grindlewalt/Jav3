import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function Skills() {
  const [skills, setSkills] = useState([])
  const [selected, setSelected] = useState(null)
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [error, setError] = useState(null)

  const refresh = () => api('/api/skills').then((r) => setSkills(r.skills))
  useEffect(() => { refresh() }, [])

  useEffect(() => {
    if (!selected) return
    api(`/api/skills/${selected}`).then((r) => { setContent(r.content); setDirty(false) })
  }, [selected])

  async function create(e) {
    e.preventDefault()
    setError(null)
    try {
      const r = await api('/api/skills', {
        method: 'POST',
        body: JSON.stringify({ name, description: desc || undefined }),
      })
      setName(''); setDesc('')
      await refresh()
      setSelected(r.slug)
    } catch (err) { setError(err.detail) }
  }

  async function save() {
    await api(`/api/skills/${selected}`, {
      method: 'PUT', body: JSON.stringify({ content }) })
    setDirty(false)
    refresh()
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Skills</div>
        <form className="stack" onSubmit={create}>
          <input placeholder="new skill name" value={name} required
                 onChange={(e) => setName(e.target.value)} />
          <input placeholder="what does it do?" value={desc}
                 onChange={(e) => setDesc(e.target.value)} />
          <button type="submit">Create</button>
          {error && <span className="error">{error}</span>}
        </form>
        <ul className="file-list">
          {skills.map((s) => (
            <li key={s.slug} className={selected === s.slug ? 'active' : ''}
                onClick={() => setSelected(s.slug)}>
              {s.name}
              <span className="tag">{s.enabled ? 'granted' : 'not granted'}</span>
            </li>
          ))}
          {skills.length === 0 && <li className="dim">none yet</li>}
        </ul>
        <p className="dim small">a skill is a tool with references — markdown +
          frontmatter, compiled into the registry. `enabled: false` keeps it
          catalogued but not granted to Jarvis.</p>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">select or create a skill — SKILL.md opens here</div>
        ) : (
          <>
            <div className="pane-head">
              <h3>skills/{selected}/SKILL.md</h3>
              <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            <textarea className="md-editor grow" spellCheck={false} value={content}
                      onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
          </>
        )}
      </main>
    </div>
  )
}
