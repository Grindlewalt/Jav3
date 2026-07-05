import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Everything is INCLUDED by default; checkboxes remove. That way an agent
// can't silently miss something necessary — you only take away what it
// shouldn't need.
export default function Agents() {
  const [agents, setAgents] = useState([])
  const [selected, setSelected] = useState(null)
  const [agent, setAgent] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [contextItems, setContextItems] = useState([])
  const [toolItems, setToolItems] = useState([])
  const [skillItems, setSkillItems] = useState([])
  const nameRef = useRef(null)

  const refresh = () => api('/api/agents').then((r) => setAgents(r.agents))
  useEffect(() => {
    refresh()
    api('/api/memory').then((r) => setContextItems([
      ...r.files.filter((f) => f.path.endsWith('.md')).map((f) => f.path),
      'active-project',
    ]))
    api('/api/tools').then((r) => setToolItems(r.tools.map((t) => t.name)))
    api('/api/skills').then((r) => setSkillItems(r.skills.map((s) => s.name)))
  }, [])

  useEffect(() => {
    if (!selected) { setAgent(null); return }
    api(`/api/agents/${selected}`).then((a) => { setAgent(a); setDirty(false) })
  }, [selected])

  // n = new agent, when not typing
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return
      if (e.key.toLowerCase() === 'n' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault()
        nameRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  async function create(e) {
    e.preventDefault()
    const name = nameRef.current.value.trim()
    if (!name) return
    try {
      const r = await api('/api/agents', {
        method: 'POST', body: JSON.stringify({ name }) })
      nameRef.current.value = ''
      await refresh()
      setSelected(r.slug)
    } catch (err) { window.alert(err.detail || err) }
  }

  const patch = (p) => { setAgent((a) => ({ ...a, ...p })); setDirty(true) }

  const toggleExclude = (field, item) => {
    const list = agent[field] || []
    patch({
      [field]: list.includes(item)
        ? list.filter((x) => x !== item)
        : [...list, item],
    })
  }

  async function save() {
    await api(`/api/agents/${selected}`, {
      method: 'PUT', body: JSON.stringify(agent) })
    setDirty(false)
    refresh()
  }

  async function del() {
    if (!window.confirm(`delete agent "${selected}"?`)) return
    await api(`/api/agents/${selected}`, { method: 'DELETE' })
    setSelected(null)
    refresh()
  }

  function ExcludeList({ title, items, field, hint }) {
    if (items.length === 0) return (
      <div className="agent-section">
        <div className="side-title">{title}</div>
        <span className="dim small">{hint}</span>
      </div>
    )
    return (
      <div className="agent-section">
        <div className="side-title">{title}</div>
        <div className="check-grid">
          {items.map((item) => {
            const excluded = (agent[field] || []).includes(item)
            return (
              <label key={item} className={excluded ? 'excluded' : ''}>
                <input type="checkbox" checked={!excluded}
                       onChange={() => toggleExclude(field, item)} />
                <span>{item}</span>
              </label>
            )
          })}
        </div>
      </div>
    )
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Agents</div>
        <form className="row" onSubmit={create}>
          <input ref={nameRef} className="grow" placeholder="new agent name  (n)" />
          <button type="submit">+</button>
        </form>
        <ul className="file-list">
          {agents.map((a) => (
            <li key={a.slug} className={selected === a.slug ? 'active' : ''}
                onClick={() => setSelected(a.slug)}>
              {a.name}
              {a.model && <span className="tag">{a.model}</span>}
            </li>
          ))}
          {agents.length === 0 && <li className="dim">none yet — press n</li>}
        </ul>
        <p className="dim small">definitions only for now — the spawn tool that
          runs these lands with the tool layer. Everything is included by
          default; untick to exclude.</p>
      </aside>
      <main className="editor-pane">
        {!agent ? (
          <div className="dim center-pad">select an agent, or press <kbd>n</kbd> to create one</div>
        ) : (
          <div className="agent-form">
            <div className="pane-head">
              <h3>{agent.name}</h3>
              <button className="ghost danger" onClick={del}>delete</button>
              <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            <div className="field-row">
              <label>name
                <input value={agent.name} onChange={(e) => patch({ name: e.target.value })} />
              </label>
              <label>description
                <input value={agent.description}
                       onChange={(e) => patch({ description: e.target.value })} />
              </label>
            </div>
            <div className="field-row">
              <label>model
                <input value={agent.model} placeholder="inherit (deepseek-v4-flash)"
                       onChange={(e) => patch({ model: e.target.value })} />
              </label>
              <label>base url
                <input value={agent.base_url}
                       placeholder="default DeepSeek · ollama: http://localhost:11434/v1"
                       onChange={(e) => patch({ base_url: e.target.value })} />
              </label>
            </div>
            <label className="prompt-label">system prompt
              <textarea className="md-editor" rows={7} spellCheck={false}
                        value={agent.prompt}
                        onChange={(e) => patch({ prompt: e.target.value })} />
            </label>
            <ExcludeList title="context (untick to exclude)" items={contextItems}
                         field="context_exclude" />
            <ExcludeList title="tools (untick to exclude)" items={toolItems}
                         field="tools_exclude"
                         hint="registry is empty — grants appear here as tools land" />
            <ExcludeList title="skills (untick to exclude)" items={skillItems}
                         field="skills_exclude" hint="no skills yet" />
            <label className="own-memory">
              <input type="checkbox" checked={agent.own_memory}
                     onChange={(e) => patch({ own_memory: e.target.checked })} />
              <span>own memory — agent keeps its own notes instead of writing to
                shared memory <span className="dim">(experimental, semantics land
                with the tool layer)</span></span>
            </label>
          </div>
        )}
      </main>
    </div>
  )
}
