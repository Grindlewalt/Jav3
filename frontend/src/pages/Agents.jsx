import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'

// Everything is INCLUDED by default; checkboxes remove. That way an agent
// can't silently miss something necessary — you only take away what it
// shouldn't need.
export default function Agents() {
  const [agents, setAgents] = useState([])
  const [trash, setTrash] = useState([])
  const [selected, setSelected] = useState(null)
  const [agent, setAgent] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [contextItems, setContextItems] = useState([])
  const [toolItems, setToolItems] = useState([])
  const [skillItems, setSkillItems] = useState([])
  const [quiz, setQuiz] = useState(null)        // [{question, kind, options, answer}]
  const [genBusy, setGenBusy] = useState(false)
  const [secrets, setSecrets] = useState([])
  const nameRef = useRef(null)

  const refresh = () => {
    api('/api/agents').then((r) => setAgents(r.agents))
    api('/api/agents/trash').then((r) => setTrash(r.agents))
  }
  useEffect(() => {
    refresh()
    api('/api/memory').then((r) => setContextItems([
      ...r.files.filter((f) => f.path.endsWith('.md')).map((f) => f.path),
      'active-project',
    ]))
    api('/api/tools').then((r) => setToolItems(r.tools.map((t) => t.name)))
    api('/api/skills').then((r) => setSkillItems(r.skills.map((s) => s.name)))
    api('/api/secrets').then((r) => setSecrets(r.secrets)).catch(() => {})
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
    let name = nameRef.current.value.trim()
    if (!name) {
      // clicking + with an empty field should ask, not silently do nothing
      name = (window.prompt('name the new agent') || '').trim()
      if (!name) { nameRef.current?.focus(); return }
    }
    try {
      const r = await api('/api/agents', {
        method: 'POST', body: JSON.stringify({ name }) })
      nameRef.current.value = ''
      await refresh()
      setSelected(r.slug)
    } catch (err) { notifyError(err) }
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

  function setAnswer(i, answer) {
    setQuiz((qz) => qz.map((q, j) => (j === i ? { ...q, answer } : q)))
  }

  async function startQuiz() {
    const description = agent.description?.trim()
      || window.prompt('one sentence: what should this agent do?')
    if (!description) return
    if (!agent.description?.trim()) patch({ description })
    setGenBusy(true)
    try {
      const r = await api('/api/agents/prompt-quiz', {
        method: 'POST', body: JSON.stringify({ description }) })
      setQuiz(r.questions.map((q) => ({ ...q, answer: q.kind === 'multi' ? [] : '' })))
    } catch (err) { notifyError(err) }
    setGenBusy(false)
  }

  async function generatePrompt() {
    setGenBusy(true)
    try {
      const answers = quiz.map((q) => ({
        question: q.question,
        answer: Array.isArray(q.answer) ? q.answer.join(', ') : q.answer,
      }))
      const r = await api('/api/agents/prompt-generate', {
        method: 'POST',
        body: JSON.stringify({ description: agent.description, answers }) })
      patch({ prompt: r.prompt })
      setQuiz(null)
    } catch (err) { notifyError(err) }
    setGenBusy(false)
  }

  async function save() {
    await api(`/api/agents/${selected}`, {
      method: 'PUT', body: JSON.stringify(agent) })
    setDirty(false)
    refresh()
  }

  async function del() {
    if (!window.confirm(`move agent "${selected}" to trash?`)) return
    await api(`/api/agents/${selected}`, { method: 'DELETE' })
    setSelected(null)
    refresh()
  }

  async function restore(slug) {
    try {
      await api(`/api/agents/${slug}/restore`, { method: 'POST' })
      refresh()
    } catch (err) { notifyError(err) }
  }
  async function purge(slug) {
    if (!window.confirm(`permanently delete "${slug}"? this can't be undone`)) return
    await api(`/api/agents/${slug}/purge`, { method: 'DELETE' })
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
        {trash.length > 0 && (
          <details className="trash-bin">
            <summary>Recently deleted ({trash.length})</summary>
            <ul className="file-list">
              {trash.map((a) => (
                <li key={a.slug} className="trashed">
                  <span className="grow ellipsis">{a.name}</span>
                  <button className="win-btn" title="restore"
                          onClick={() => restore(a.slug)}>↺</button>
                  <button className="win-btn" title="delete forever"
                          onClick={() => purge(a.slug)}>×</button>
                </li>
              ))}
            </ul>
          </details>
        )}
        <p className="dim small">run an agent from a project board (Run an agent
          panel), on a schedule, or have Jarvis summon one in chat. Everything
          is included by default; untick to exclude.</p>
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
              <span className="row" style={{ float: 'right', gap: 6 }}>
                <button className="ghost" type="button" disabled={genBusy}
                        title="answer a short quiz, get a generated prompt"
                        onClick={startQuiz}>{genBusy ? '…' : '✨ generate'}</button>
              </span>
              <textarea className="md-editor" rows={7} spellCheck={false}
                        value={agent.prompt}
                        onChange={(e) => patch({ prompt: e.target.value })} />
              {secrets.length > 0 && (
                <div className="dim small" style={{ marginTop: 4 }}>
                  API keys — click to reference (the agent uses the key, never
                  sees its value):{' '}
                  {secrets.map((s) => (
                    <button key={s.name} type="button" className="ghost"
                            style={{ marginRight: 4 }}
                            title={s.hosts?.length
                              ? `usable in web_read on ${s.hosts.join(', ')}`
                              : 'unusable — bind web hosts on the Context page to allow web_read'}
                            onClick={() => patch({ prompt:
                              `${agent.prompt.trimEnd()}\n{{secret:${s.name}}}` })}>
                      {`{{secret:${s.name}}}`}
                    </button>
                  ))}
                  — new keys are added in the Secrets panel on the Context page.
                </div>
              )}
            </label>
            {quiz && (
              <div className="quiz">
                <div className="side-title">quick quiz — answers shape the prompt</div>
                {quiz.map((q, i) => (
                  <div key={i} className="quiz-q">
                    <div>{q.question}</div>
                    {q.kind === 'short' ? (
                      <input placeholder="short answer…" value={q.answer || ''}
                             onChange={(e) => setAnswer(i, e.target.value)} />
                    ) : q.options.map((o) => (
                      <label key={o} className="quiz-opt">
                        <input
                          type={q.kind === 'multi' ? 'checkbox' : 'radio'}
                          name={`q${i}`}
                          checked={q.kind === 'multi'
                            ? (q.answer || []).includes(o) : q.answer === o}
                          onChange={() => q.kind === 'multi'
                            ? setAnswer(i, (q.answer || []).includes(o)
                                ? (q.answer || []).filter((x) => x !== o)
                                : [...(q.answer || []), o])
                            : setAnswer(i, o)} />
                        <span>{o}</span>
                      </label>
                    ))}
                  </div>
                ))}
                <div className="row">
                  <button type="button" disabled={genBusy} onClick={generatePrompt}>
                    {genBusy ? 'writing…' : 'write the prompt'}</button>
                  <button type="button" className="ghost" onClick={() => setQuiz(null)}>cancel</button>
                </div>
              </div>
            )}
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
