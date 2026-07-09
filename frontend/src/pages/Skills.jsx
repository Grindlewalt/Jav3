import { useEffect, useState } from 'react'
import { api } from '../api.js'

// Skill authoring as a fill-out form: the fields generate valid frontmatter
// server-side, so nobody hand-writes YAML. "Edit raw" stays as the escape
// hatch for anything the form doesn't cover.
const BLANK_PARAM = { name: '', type: 'string', description: '', required: false }

export default function Skills() {
  const [skills, setSkills] = useState([])
  const [selected, setSelected] = useState(null)
  const [fields, setFields] = useState(null)
  const [content, setContent] = useState('')
  const [raw, setRaw] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [error, setError] = useState(null)

  const refresh = () => api('/api/skills').then((r) => setSkills(r.skills))
  useEffect(() => { refresh() }, [])

  useEffect(() => {
    if (!selected) return
    api(`/api/skills/${selected}`).then((r) => {
      setContent(r.content)
      setFields(r.fields)
      setDirty(false)
    })
  }, [selected])

  const set = (p) => { setFields((f) => ({ ...f, ...p })); setDirty(true) }
  const setParam = (i, p) => set({
    params: fields.params.map((x, j) => (j === i ? { ...x, ...p } : x)) })

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
    if (raw) {
      await api(`/api/skills/${selected}`, {
        method: 'PUT', body: JSON.stringify({ content }) })
    } else {
      await api(`/api/skills/${selected}/fields`, {
        method: 'PUT', body: JSON.stringify(fields) })
    }
    setDirty(false)
    // reload both representations so switching views stays consistent
    const r = await api(`/api/skills/${selected}`)
    setContent(r.content); setFields(r.fields)
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
        <p className="dim small">a skill teaches Jarvis a procedure: it sees the
          name + "use when" every turn, and gets the full instructions only when
          it invokes the skill.</p>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">select or create a skill</div>
        ) : (
          <>
            <div className="pane-head">
              <h3>{selected}</h3>
              <button className="ghost" onClick={() => setRaw((v) => !v)}>
                {raw ? 'form editor' : 'edit raw'}</button>
              <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            {raw || !fields ? (
              <textarea className="md-editor grow" spellCheck={false} value={content}
                        onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
            ) : (
              <div className="skill-form">
                <label className="mini">what it does (shown to Jarvis every turn)
                  <input value={fields.description}
                         onChange={(e) => set({ description: e.target.value })} />
                </label>
                <label className="mini">use when… (how Jarvis decides to pick it)
                  <input value={fields.when_to_use}
                         onChange={(e) => set({ when_to_use: e.target.value })} />
                </label>
                <label className="mini row" style={{ alignItems: 'center' }}>
                  <input type="checkbox" checked={fields.enabled}
                         onChange={(e) => set({ enabled: e.target.checked })} />
                  <span>granted to Jarvis and agents</span>
                </label>
                <label className="mini">instructions (loaded when the skill is invoked)
                  <textarea rows={12} className="md-editor" spellCheck={false}
                            value={fields.body}
                            onChange={(e) => set({ body: e.target.value })} />
                </label>
                <div className="mini dim">arguments (optional)</div>
                {fields.params.map((p, i) => (
                  <div className="row" key={i}>
                    <input placeholder="name" value={p.name} style={{ width: 110 }}
                           onChange={(e) => setParam(i, { name: e.target.value })} />
                    <select value={p.type}
                            onChange={(e) => setParam(i, { type: e.target.value })}>
                      {['string', 'number', 'boolean', 'array'].map((t) =>
                        <option key={t} value={t}>{t}</option>)}
                    </select>
                    <input className="grow" placeholder="description" value={p.description}
                           onChange={(e) => setParam(i, { description: e.target.value })} />
                    <label className="dim small" title="required">
                      <input type="checkbox" checked={p.required}
                             onChange={(e) => setParam(i, { required: e.target.checked })} />req
                    </label>
                    <button className="win-btn" type="button"
                            onClick={() => set({ params: fields.params.filter((_, j) => j !== i) })}>×</button>
                  </div>
                ))}
                <button className="ghost" type="button"
                        onClick={() => set({ params: [...fields.params, { ...BLANK_PARAM }] })}>
                  + argument</button>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  )
}
