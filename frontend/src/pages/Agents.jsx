import { useEffect, useRef, useState } from 'react'
import { Link, Outlet } from 'react-router-dom'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { modelOption, useModel } from '../modelInfo.js'
import Page from '../components/Page.jsx'
import Tabs from '../components/Tabs.jsx'
import Button, { SaveButton } from '../components/Button.jsx'
import Input from '../components/Input.jsx'
import Toggle from '../components/Toggle.jsx'
import Select from '../components/Select.jsx'
import Tag from '../components/Tag.jsx'
import Toolbar from '../components/Toolbar.jsx'
import EmptyState from '../components/EmptyState.jsx'

// The Agents page is a layout route: this shell owns the one <h1> and the tab
// strip, and each tab is a real URL (/agents, /agents/skills,
// /agents/outputs[/:slug]) rendered through the Outlet. `split` because two of
// the three tabs are a list + editor; the Outputs tab is a lone <main>.
export default function Agents() {
  return (
    <Page variant="split" title="Agents"
          actions={(
            <Tabs label="Agents sections" items={[
              { to: '/agents', end: true, label: 'Definitions' },
              { to: '/agents/skills', label: 'Skills' },
              { to: '/agents/outputs', label: 'Outputs' },
            ]} />
          )}>
      <Outlet />
    </Page>
  )
}

// The definitions tab. An agent gets EVERYTHING by default — context, tools,
// skills — so the editor no longer offers the context/tools untick lists: a
// necessary piece can't be forgotten, only a skill knowingly taken away. The
// old *_exclude fields still round-trip untouched on save (hand-edited
// AGENT.md files and the funnel may set them; the server still honours them).
export function AgentDefinitions() {
  const [agents, setAgents] = useState([])
  const [trash, setTrash] = useState([])
  const [selected, setSelected] = useState(null)
  const [agent, setAgent] = useState(null)
  const ask = useAsk()
  // an agent with no model resolves pin > runtime override > default, so it
  // inherits `active`, not `default` — shown by its configured label, the
  // way every other model picker names it, never the raw id
  const modelInfo = useModel()
  const inheritedModel = modelInfo?.active
    ? modelOption(modelInfo, modelInfo.active).label : ''
  const [dirty, setDirty] = useState(false)
  const [skillItems, setSkillItems] = useState([])
  const [projects, setProjects] = useState([])
  const [quiz, setQuiz] = useState(null)        // [{question, kind, options, answer}]
  const [genBusy, setGenBusy] = useState(false)
  const [secrets, setSecrets] = useState([])
  const nameRef = useRef(null)
  const editorRef = useRef(null)

  const refresh = () => {
    api('/api/agents').then((r) => setAgents(r.agents))
    api('/api/agents/trash').then((r) => setTrash(r.agents))
  }
  useEffect(() => {
    refresh()
    api('/api/skills').then((r) => setSkillItems(r.skills.map((s) => s.name)))
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {})
    api('/api/secrets').then((r) => setSecrets(r.secrets)).catch(() => {})
  }, [])

  useEffect(() => {
    setQuiz(null)
    if (!selected) { setAgent(null); return }
    api(`/api/agents/${selected}`).then((a) => { setAgent(a); setDirty(false) })
  }, [selected])

  // On a phone the editor stacks under the roster, below the fold: picking an
  // agent there has to bring its editor up, or the tap looks like it did nothing
  useEffect(() => {
    if (!agent || !window.matchMedia('(max-width: 768px)').matches) return
    editorRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [agent?.slug]) // eslint-disable-line

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
      name = (await ask.prompt('Name the new agent', '',
                               { confirmLabel: 'Create' }) || '').trim()
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

  const toggleSkill = (name) => {
    const list = agent.skills_exclude || []
    patch({ skills_exclude: list.includes(name)
      ? list.filter((x) => x !== name) : [...list, name] })
  }

  function setAnswer(i, answer) {
    setQuiz((qz) => qz.map((q, j) => (j === i ? { ...q, answer } : q)))
  }

  async function startQuiz() {
    const description = agent.description?.trim()
      || await ask.prompt('One sentence: what should this agent do?', '',
                          { confirmLabel: 'Continue' })
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
    try {
      // the whole definition goes back, including the exclusion lists this
      // editor no longer shows — omitting them would reset them to [] and
      // silently widen an agent someone narrowed by hand
      await api(`/api/agents/${selected}`, {
        method: 'PUT', body: JSON.stringify(agent) })
      setDirty(false)
      refresh()
    } catch (err) { notifyError(err) }   // e.g. 400: the project no longer exists
  }

  async function del() {
    if (!await ask.confirm(`Move agent "${selected}" to trash?`,
                           { confirmLabel: 'Move to trash' })) return
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
    if (!await ask.confirm(`Permanently delete "${slug}"?`,
                           { body: "This can't be undone.",
                             confirmLabel: 'Delete forever', danger: true })) return
    await api(`/api/agents/${slug}/purge`, { method: 'DELETE' })
    refresh()
  }

  // a binding to a project that has since been deleted still shows as itself,
  // so the editor doesn't quietly display "none" over a stale value
  const projectOptions = [
    { value: '', label: 'no project — follows whoever starts it' },
    ...projects.map((p) => ({ value: p.slug, label: p.name })),
    ...(agent?.project && !projects.some((p) => p.slug === agent.project)
      ? [{ value: agent.project, label: `${agent.project} (missing)` }] : []),
  ]
  const projectName = (slug) => projects.find((p) => p.slug === slug)?.name || slug

  return (
    <>
      <aside>
        <form className="row agent-new" onSubmit={create}>
          <input ref={nameRef} className="grow" placeholder="new agent name  (n)"
                 aria-label="new agent name" />
          <Button type="submit" aria-label="create agent">+</Button>
        </form>
        <ul className="agent-list">
          {agents.map((a) => (
            <li key={a.slug}>
              <button type="button" aria-current={selected === a.slug || undefined}
                      className={selected === a.slug ? 'agent-row active' : 'agent-row'}
                      onClick={() => setSelected(a.slug)}>
                <span className="agent-row-name">{a.name}</span>
                {(a.project || a.model) && (
                  <span className="agent-row-sub">
                    {a.project && <Tag>{projectName(a.project)}</Tag>}
                    {a.model && <Tag>{a.model}</Tag>}
                  </span>
                )}
              </button>
            </li>
          ))}
          {agents.length === 0 && <EmptyState as="li">none yet — press n</EmptyState>}
        </ul>
        {trash.length > 0 && (
          <details className="deleted-fold trash-bin">
            <summary>
              Recently deleted ({trash.length})
              <span className="chev" aria-hidden="true">›</span>
            </summary>
            <ul className="trash-list">
              {trash.map((a) => (
                <li key={a.slug}>
                  <span className="grow ellipsis">{a.name}</span>
                  <button className="win-btn" title="restore" aria-label={`restore ${a.name}`}
                          onClick={() => restore(a.slug)}>↺</button>
                  <button className="win-btn" title="delete forever"
                          aria-label={`delete ${a.name} forever`}
                          onClick={() => purge(a.slug)}>×</button>
                </li>
              ))}
            </ul>
          </details>
        )}
        <p className="dim small">talk to an agent from a project board's chat panel,
          give it a one-off task in the Run an agent panel, put it on a schedule, or
          have Jav3 summon one in chat.</p>
      </aside>
      <main ref={editorRef} className={agent ? 'editor-pane' : 'editor-pane split-idle'}>
        {!agent ? (
          <EmptyState pad>select an agent, or press <kbd>n</kbd> to create one</EmptyState>
        ) : (
          <div className="agent-form">
            <Toolbar variant="pane" title={agent.name}>
              <Link className="agent-outputs-link" to={`/agents/outputs/${agent.slug}`}>
                outputs</Link>
              <Button variant="ghost" danger onClick={del}>Delete</Button>
              <SaveButton dirty={dirty} onSave={save} />
            </Toolbar>
            <div className="field-row">
              <Input label="name" value={agent.name}
                     onChange={(e) => patch({ name: e.target.value })} />
              <Input label="description" value={agent.description}
                     onChange={(e) => patch({ description: e.target.value })} />
            </div>
            <div className="field-row">
              <Select label="works in" value={agent.project || ''} options={projectOptions}
                      hint="its runs and threads start in this project unless the one
                        starting it names another"
                      onChange={(e) => patch({ project: e.target.value })} />
              <Input label="max rounds" type="number" min="0" max="200"
                     className="agent-num" value={agent.max_iterations ?? 0}
                     hint="tool-calling rounds per run; 0 = the default for how it
                       was started"
                     onChange={(e) => patch({
                       max_iterations: Math.max(0, parseInt(e.target.value, 10) || 0) })} />
            </div>
            <div className="field-row">
              <Input label="model" value={agent.model}
                     placeholder={`inherit (${inheritedModel || 'default'})`}
                     onChange={(e) => patch({ model: e.target.value })} />
              <Input label="base url" value={agent.base_url}
                     placeholder="default endpoint · ollama: http://<host>:11434/v1"
                     onChange={(e) => patch({ base_url: e.target.value })} />
            </div>
            <label className="prompt-label">
              <span className="row prompt-head">
                <span className="grow">system prompt</span>
                <Button variant="ghost" disabled={genBusy}
                        title="answer a short quiz, get a generated prompt"
                        onClick={startQuiz}>{genBusy ? '…' : 'Generate'}</Button>
              </span>
              <textarea className="md-editor" rows={7} spellCheck={false}
                        value={agent.prompt}
                        onChange={(e) => patch({ prompt: e.target.value })} />
              {secrets.length > 0 && (
                <div className="dim small secret-refs">
                  API keys — click to reference (the agent uses the key, never
                  sees its value):{' '}
                  {secrets.map((s) => (
                    <Button key={s.name} variant="ghost"
                            title={s.hosts?.length
                              ? `usable in web_read on ${s.hosts.join(', ')}`
                              : 'unusable — bind web hosts in Review → Secrets to allow web_read'}
                            onClick={() => patch({ prompt:
                              `${agent.prompt.trimEnd()}\n{{secret:${s.name}}}` })}>
                      {`{{secret:${s.name}}}`}
                    </Button>
                  ))}
                  — new keys are added in Review → Secrets.
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
                  <Button disabled={genBusy} onClick={generatePrompt}>
                    {genBusy ? 'writing…' : 'Write the prompt'}</Button>
                  <Button variant="ghost" onClick={() => setQuiz(null)}>Cancel</Button>
                </div>
              </div>
            )}
            <SkillPicker items={skillItems} excluded={agent.skills_exclude || []}
                         onToggle={toggleSkill} />
            <OwnMemory slug={agent.slug} on={!!agent.own_memory}
                       onChange={(v) => patch({ own_memory: v })} />
          </div>
        )}
      </main>
    </>
  )
}

// Skills are the one thing still worth taking away per agent: every skill is
// listed on every turn, and a narrow agent pays for a catalogue it never uses.
// A pressed chip is a skill the agent keeps; unpressing removes it.
function SkillPicker({ items, excluded, onToggle }) {
  return (
    <div className="agent-section">
      <div className="side-title" id="agent-skills-h">skills</div>
      {items.length === 0 ? (
        <EmptyState>no skills yet — add one in the Skills tab</EmptyState>
      ) : (
        <>
          <div className="skill-chips" role="group" aria-labelledby="agent-skills-h">
            {items.map((name) => {
              const kept = !excluded.includes(name)
              return (
                <button key={name} type="button" aria-pressed={kept}
                        className={kept ? 'skill-chip' : 'skill-chip off'}
                        title={kept ? 'click to take this skill away' : 'click to give it back'}
                        onClick={() => onToggle(name)}>{name}</button>
              )
            })}
          </div>
          <span className="field-hint">
            {excluded.length
              ? `${excluded.length} of ${items.length} taken away`
              : 'every skill — click one to take it away'}</span>
        </>
      )}
    </div>
  )
}

// own_memory redirects the agent's memory_read/write to agents/<slug>/memory/.
// A silo nobody can read would be worse than none, so the notes are one
// disclosure away; they load when it opens, not with the editor.
function OwnMemory({ slug, on, onChange }) {
  const [notes, setNotes] = useState(null)
  useEffect(() => { setNotes(null) }, [slug])
  const load = (e) => {
    if (!e.currentTarget.open || notes) return
    api(`/api/agents/${slug}/memory`).then((r) => setNotes(r.notes))
      .catch(() => setNotes([]))
  }
  return (
    <div className="agent-section">
      <div className="agent-toggles">
        <Toggle checked={on} onChange={onChange} label="own memory"
                onText="own memory" offText="own memory" />
        <span className="field-hint">keeps its notes to itself instead of writing to
          the shared notes — the operator's standing notes still lead its prompt</span>
      </div>
      <details className="agent-notes" onToggle={load}>
        <summary>its private notes</summary>
        {notes === null ? <span className="dim small">loading…</span>
          : notes.length === 0 ? <EmptyState>none yet</EmptyState>
          : (
            <ul className="agent-notes-list">
              {notes.map((n) => (
                <li key={n.name}>
                  <details>
                    <summary><code>{n.name}</code>
                      {n.description && <span className="dim"> — {n.description}</span>}
                    </summary>
                    <pre className="agent-note-body">{n.body}</pre>
                  </details>
                </li>
              ))}
            </ul>
          )}
      </details>
    </div>
  )
}
