import { useEffect, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { api, chatStream } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { ago } from '../format.js'
import { modelOption, useModel } from '../modelInfo.js'
import Page from '../components/Page.jsx'
import Tabs from '../components/Tabs.jsx'
import Button, { SaveButton } from '../components/Button.jsx'
import Card from '../components/Card.jsx'
import Input from '../components/Input.jsx'
import Select from '../components/Select.jsx'
import Tag from '../components/Tag.jsx'
import Toggle from '../components/Toggle.jsx'
import Menu, { MenuItem } from '../components/Menu.jsx'
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

// The definitions tab: a roster on the left, one agent on the right. The
// prompt is the page; everything else is one compact Settings card under it.
// Save PUTs the whole definition back — every omitted field resets server-side
// — so the fields this view doesn't show (description, base_url, the legacy
// *_exclude lists) round-trip untouched.
const NEW_ID = 'agent-new-name'

export function AgentDefinitions() {
  const [agents, setAgents] = useState([])
  const [trash, setTrash] = useState([])
  const [projects, setProjects] = useState([])
  const [skills, setSkills] = useState([])
  const [newName, setNewName] = useState('')
  const [selected, setSelected] = useState(null)
  const [agent, setAgent] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [last, setLast] = useState(null)
  const [run, setRun] = useState(null)          // {slug, id} while a run streams
  const [menu, setMenu] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const ask = useAsk()
  const modelInfo = useModel()
  const inherited = modelInfo ? modelOption(modelInfo, modelInfo.active).label : ''

  const refresh = () => {
    api('/api/agents').then((r) => setAgents(r.agents))
    api('/api/agents/trash').then((r) => setTrash(r.agents))
  }
  useEffect(() => {
    refresh()
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {})
    api('/api/skills').then((r) => setSkills((r.skills || []).map((s) => s.name)))
      .catch(() => {})
  }, [])

  const loadLast = (slug) =>
    api(`/api/agents/${slug}/outputs?limit=1`)
      .then((r) => setLast(r.outputs?.[0] ? { ...r.outputs[0], of: slug } : null))
      .catch(() => setLast(null))

  useEffect(() => {
    setLast(null)
    if (!selected) { setAgent(null); return }
    api(`/api/agents/${selected}`).then((a) => { setAgent(a); setDirty(false) })
    loadLast(selected)
  }, [selected])

  // n = new agent, when not typing
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT') return
      if (e.key.toLowerCase() === 'n' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault()
        document.getElementById(NEW_ID)?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // switching agents (or going back on a phone) must not drop an unsaved edit
  async function pick(slug) {
    if (slug === selected) return
    if (dirty && !await ask.confirm('Discard unsaved changes?',
                                    { confirmLabel: 'Discard', danger: true })) return
    setDirty(false)
    setSelected(slug)
  }

  async function create(e) {
    e.preventDefault()
    let name = newName.trim()
    if (!name) {
      // + with an empty field asks, rather than silently doing nothing
      name = (await ask.prompt('Name the new agent', '',
                               { confirmLabel: 'Create' }) || '').trim()
      if (!name) return
    }
    try {
      const r = await api('/api/agents', {
        method: 'POST', body: JSON.stringify({ name }) })
      setNewName('')
      refresh()
      setDirty(false)
      setSelected(r.slug)
    } catch (err) { notifyError(err) }
  }

  const patch = (p) => { setAgent((a) => ({ ...a, ...p })); setDirty(true) }

  async function save() {
    try {
      await api(`/api/agents/${agent.slug}`, {
        method: 'PUT', body: JSON.stringify(agent) })
      setDirty(false)
      refresh()
      return true
    } catch (err) { notifyError(err); return false }   // e.g. 400: missing project
  }

  async function rename() {
    setMenu(false)
    const name = (await ask.prompt('Rename agent', agent.name,
                                   { confirmLabel: 'Rename' }) || '').trim()
    if (name && name !== agent.name) patch({ name })
  }

  async function del() {
    setMenu(false)
    if (!await ask.confirm(`Move agent "${agent.slug}" to trash?`,
                           { confirmLabel: 'Move to trash' })) return
    await api(`/api/agents/${agent.slug}`, { method: 'DELETE' })
    setDirty(false)
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

  // A run is detached server-side: this only starts it and holds its id for
  // Stop. It is deliberately not watchRun()-registered, so the completion
  // notice toasts; the transcript is on the Outputs tab.
  async function start(task, confirmPeak = false) {
    const slug = agent.slug
    if (!task) {
      task = (await ask.prompt('Task', '', { confirmLabel: 'Run' }) || '').trim()
      if (!task) return
      if (dirty && !await save()) return
    }
    setRun({ slug, id: null })
    try {
      await chatStream({ task, confirm_peak: confirmPeak }, (ev) => {
        if (ev.type === 'start') {
          setRun({ slug, id: ev.conversation_id })
          loadLast(slug)
        }
        if (ev.type === 'error') notifyError(new Error(ev.message))
      }, `/api/agents/${slug}/run`)
    } catch (err) {
      setRun(null)
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        if (await ask.confirm('Peak pricing window — run anyway?',
                              { confirmLabel: 'Run' })) return start(task, true)
        return
      }
      notifyError(err)
    }
    setRun(null)
    loadLast(slug)
  }

  async function stop() {
    if (!run?.id) return
    try { await api(`/api/agents/runs/${run.id}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }

  // one call, no quiz: the description in, a whole prompt out
  async function draft() {
    const description = (await ask.prompt('What should this agent do?',
      agent.description || '', { confirmLabel: 'Draft' }) || '').trim()
    if (!description) return
    setDrafting(true)
    try {
      const r = await api('/api/agents/prompt-generate', {
        method: 'POST', body: JSON.stringify({ description, answers: [] }) })
      patch({ description, prompt: r.prompt })
    } catch (err) { notifyError(err) }
    setDrafting(false)
  }

  const projectName = (slug) => projects.find((p) => p.slug === slug)?.name || slug
  // a binding to a since-deleted project still shows as itself, or Save would
  // quietly unbind it
  const projectOptions = [
    { value: '', label: 'any' },
    ...projects.map((p) => ({ value: p.slug, label: p.name })),
    ...(agent?.project && !projects.some((p) => p.slug === agent.project)
      ? [{ value: agent.project, label: `${agent.project} (missing)` }] : []),
  ]
  const off = agent?.skills_exclude || []
  const toggleSkill = (name) => patch({
    skills_exclude: off.includes(name) ? off.filter((n) => n !== name) : [...off, name] })
  // `last` is fetched per pick, so it can land after a quicker switch
  const lastHere = last && agent && last.of === agent.slug ? last : null
  const running = !!(run && agent && run.slug === agent.slug)

  return (
    <>
      <aside className={selected ? 'agents-aside picked' : 'agents-aside'}>
        <form className="agent-new" onSubmit={create}>
          <Input id={NEW_ID} placeholder="New agent" aria-label="New agent name"
                 value={newName} onChange={(e) => setNewName(e.target.value)} />
          <Button type="submit" aria-label="Create agent">+</Button>
        </form>
        <ul className="agent-list">
          {agents.map((a) => (
            <li key={a.slug}>
              <button type="button" aria-current={selected === a.slug || undefined}
                      className={selected === a.slug ? 'agent-row inline active' : 'agent-row inline'}
                      onClick={() => pick(a.slug)}>
                <span className="agent-row-name">{a.name}</span>
                {a.project && <Tag>{projectName(a.project)}</Tag>}
              </button>
            </li>
          ))}
          {agents.length === 0 && <EmptyState as="li">None yet — press n</EmptyState>}
        </ul>
        {trash.length > 0 && (
          <details className="deleted-fold">
            <summary>
              Recently deleted ({trash.length})
              <span className="chev" aria-hidden="true">›</span>
            </summary>
            <ul className="trash-list">
              {trash.map((a) => (
                <li key={a.slug}>
                  <span className="grow ellipsis">{a.name}</span>
                  <button className="win-btn" aria-label={`Restore ${a.name}`}
                          onClick={() => restore(a.slug)}>↺</button>
                  <button className="win-btn" aria-label={`Delete ${a.name} forever`}
                          onClick={() => purge(a.slug)}>×</button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </aside>
      <main className={agent ? 'editor-pane agent-detail' : 'editor-pane split-idle'}>
        {!agent ? (
          <EmptyState pad>Select an agent, or press <kbd>n</kbd></EmptyState>
        ) : (
          <>
            <div className="agent-head">
              <Button variant="ghost" className="agent-back" onClick={() => pick(null)}>
                ‹ Agents
              </Button>
              <h2 className="agent-title">{agent.name}</h2>
              {dirty && <SaveButton dirty variant="ghost" onSave={save} />}
              {running
                ? <Button disabled={!run.id} onClick={stop}>Stop ■</Button>
                : <Button disabled={!!run} onClick={() => start()}>Run ▸</Button>}
              <Menu open={menu} onClose={() => setMenu(false)} width={160} floating
                    label="Agent actions"
                    trigger={(
                      <Button variant="icon" aria-label="More" aria-haspopup="menu"
                              aria-expanded={menu} onClick={() => setMenu((v) => !v)}>⋯</Button>
                    )}>
                <MenuItem onClick={rename}>Rename</MenuItem>
                <MenuItem danger onClick={del}>Delete</MenuItem>
              </Menu>
            </div>
            {lastHere && (
              <div className="agent-last">
                Last run: {ago(lastHere.last_at || lastHere.started_at)}
                {' · '}{lastHere.running ? 'running' : 'done'}
              </div>
            )}
            <div className="agent-prompt-head">
              <label htmlFor="agent-prompt">Prompt</label>
              <Button variant="ghost" disabled={drafting} onClick={draft}>
                {drafting ? 'Drafting…' : '✨ Draft prompt'}
              </Button>
            </div>
            <textarea id="agent-prompt" className="md-editor agent-prompt" spellCheck={false}
                      value={agent.prompt} onChange={(e) => patch({ prompt: e.target.value })} />
            <Card title="Settings" className="agent-settings">
              <div className="agent-grid">
                <Select label="Works in" value={agent.project || ''} options={projectOptions}
                        onChange={(e) => patch({ project: e.target.value })} />
                <Input label="Model" value={agent.model || ''} placeholder={inherited}
                       onChange={(e) => patch({ model: e.target.value })} />
                <Input label="Max rounds" type="number" min={0} max={200}
                       value={agent.max_iterations || ''} placeholder="default"
                       onChange={(e) => patch({
                         max_iterations: Math.max(0, parseInt(e.target.value, 10) || 0) })} />
                <div className="field">
                  <span>Own memory</span>
                  <div className="agent-inline">
                    <Toggle label="Own memory" checked={!!agent.own_memory}
                            onChange={(v) => patch({ own_memory: v })} />
                    {agent.own_memory && <PrivateNotes key={agent.slug} slug={agent.slug} />}
                  </div>
                </div>
                {skills.length > 0 && (
                  <div className="field agent-wide">
                    <span>Skills off</span>
                    <SkillsOff skills={skills} off={off} onToggle={toggleSkill} />
                  </div>
                )}
              </div>
            </Card>
          </>
        )}
      </main>
    </>
  )
}

// The skills this agent does without: each a removable Tag, plus a "+" that
// opens a multi-pick of every skill (the menu stays open between picks).
function SkillsOff({ skills, off, onToggle }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="agent-inline">
      {off.map((name) => (
        <Tag key={name}>
          {name}
          <button type="button" className="tag-x" aria-label={`Give back ${name}`}
                  onClick={() => onToggle(name)}>×</button>
        </Tag>
      ))}
      <Menu open={open} onClose={() => setOpen(false)} align="left" floating
            label="Skills off"
            trigger={(
              <Button variant="ghost" aria-label="Pick skills to turn off"
                      aria-haspopup="menu" aria-expanded={open}
                      onClick={() => setOpen((v) => !v)}>+</Button>
            )}>
        {skills.map((name) => (
          <MenuItem key={name} checked={off.includes(name)} onClick={() => onToggle(name)}>
            {name}
          </MenuItem>
        ))}
      </Menu>
    </div>
  )
}

// own_memory redirects the agent's memory_read/write to agents/<slug>/memory/;
// the notes load on first open and are read-only here. Keyed by slug, so
// another agent's notes start closed and unloaded.
function PrivateNotes({ slug }) {
  const [notes, setNotes] = useState(null)
  const load = (e) => {
    if (!e.currentTarget.open || notes) return
    api(`/api/agents/${slug}/memory`).then((r) => setNotes(r.notes || []))
      .catch(() => setNotes([]))
  }
  return (
    <details className="agent-notes" onToggle={load}>
      <summary>Notes <span className="chev" aria-hidden="true">›</span></summary>
      {notes === null ? <EmptyState>Loading…</EmptyState>
        : notes.length === 0 ? <EmptyState>None yet</EmptyState>
          : notes.map((n) => (
            <details key={n.name} className="agent-note">
              <summary><code>{n.name}</code></summary>
              <pre>{n.body}</pre>
            </details>
          ))}
    </details>
  )
}
