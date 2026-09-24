import { useEffect, useState } from 'react'
import { api } from './api.js'
import Button, { SaveButton } from './components/Button.jsx'
import Input, { Checkbox } from './components/Input.jsx'
import Select from './components/Select.jsx'
import Tag, { Badge } from './components/Tag.jsx'
import Toolbar from './components/Toolbar.jsx'
import EmptyState from './components/EmptyState.jsx'

// The Skills tab of the Agents page (/agents/skills; /skills redirects here).
// Skill authoring as a fill-out form: the fields generate valid frontmatter
// server-side, so nobody hand-writes YAML. "Edit raw" stays as the escape
// hatch for anything the form doesn't cover. Rendered inside the Agents
// page's split layout, so it is an <aside> + <main> pair, not a page.
const BLANK_PARAM = { name: '', type: 'string', description: '', required: false }
const PARAM_TYPES = ['string', 'number', 'boolean', 'array']

export default function SkillsPanel() {
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
    <>
      <aside>
        <form className="stack" onSubmit={create}>
          <Input placeholder="new skill name" value={name} required
                 aria-label="new skill name" onChange={(e) => setName(e.target.value)} />
          <Input placeholder="what does it do?" value={desc}
                 aria-label="what the skill does" onChange={(e) => setDesc(e.target.value)} />
          <Button type="submit">Create</Button>
          {error && <span className="error">{error}</span>}
        </form>
        <ul className="agent-list">
          {skills.map((s) => (
            <li key={s.slug}>
              <button type="button" aria-current={selected === s.slug || undefined}
                      className={selected === s.slug ? 'agent-row active' : 'agent-row'}
                      onClick={() => setSelected(s.slug)}>
                <span className="agent-row-name">{s.name}</span>
                <span className="agent-row-sub">
                  {s.enabled ? <Badge>granted</Badge> : <Tag>not granted</Tag>}
                </span>
              </button>
            </li>
          ))}
          {skills.length === 0 && <EmptyState as="li">none yet</EmptyState>}
        </ul>
        <p className="dim small">a skill teaches Jarvis a procedure: it sees the
          name + "use when" every turn, and gets the full instructions only when
          it invokes the skill. Agents get every granted skill unless their
          definition takes one away.</p>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <EmptyState pad>select or create a skill</EmptyState>
        ) : (
          <>
            <Toolbar variant="pane" title={selected}>
              <Button variant="ghost" onClick={() => setRaw((v) => !v)}>
                {raw ? 'Form editor' : 'Edit raw'}</Button>
              <SaveButton dirty={dirty} onSave={save} />
            </Toolbar>
            {raw || !fields ? (
              <textarea className="md-editor grow" spellCheck={false} value={content}
                        aria-label="SKILL.md"
                        onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
            ) : (
              <div className="skill-form">
                <Input label="what it does (shown to Jarvis every turn)"
                       value={fields.description}
                       onChange={(e) => set({ description: e.target.value })} />
                <Input label="use when… (how Jarvis decides to pick it)"
                       value={fields.when_to_use}
                       onChange={(e) => set({ when_to_use: e.target.value })} />
                <Checkbox checked={fields.enabled} label="granted to Jarvis and agents"
                          onChange={(e) => set({ enabled: e.target.checked })} />
                <Input textarea rows={12} className="md-editor" spellCheck={false}
                       label="instructions (loaded when the skill is invoked)"
                       value={fields.body}
                       onChange={(e) => set({ body: e.target.value })} />
                <div className="field-hint">arguments (optional)</div>
                {fields.params.map((p, i) => (
                  <div className="row skill-param" key={i}>
                    <input placeholder="name" value={p.name} aria-label="argument name"
                           className="skill-param-name"
                           onChange={(e) => setParam(i, { name: e.target.value })} />
                    <Select value={p.type} options={PARAM_TYPES} aria-label="argument type"
                            onChange={(e) => setParam(i, { type: e.target.value })} />
                    <input className="grow" placeholder="description" value={p.description}
                           aria-label="argument description"
                           onChange={(e) => setParam(i, { description: e.target.value })} />
                    <Checkbox checked={p.required} label="req" title="required"
                              onChange={(e) => setParam(i, { required: e.target.checked })} />
                    <button className="win-btn" type="button" aria-label="remove argument"
                            onClick={() => set({ params: fields.params.filter((_, j) => j !== i) })}>×</button>
                  </div>
                ))}
                <Button variant="ghost"
                        onClick={() => set({ params: [...fields.params, { ...BLANK_PARAM }] })}>
                  + argument</Button>
              </div>
            )}
          </>
        )}
      </main>
    </>
  )
}
