import { useEffect, useRef, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { api, chatStream } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import Page from '../components/Page.jsx'
import Tabs from '../components/Tabs.jsx'
import Button, { SaveButton } from '../components/Button.jsx'
import Input from '../components/Input.jsx'
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

// The definitions tab, stripped to a working skeleton for the rebuild — see
// AGENTS-REBUILD.md beside this file for everything that used to be here, the
// API contract and the design constraints. Save PUTs the whole definition back,
// so fields this skeleton no longer shows (model, project, max rounds, own
// memory, the *_exclude lists) round-trip untouched.
export function AgentDefinitions() {
  const [agents, setAgents] = useState([])
  const [trash, setTrash] = useState([])
  const [selected, setSelected] = useState(null)
  const [agent, setAgent] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [runId, setRunId] = useState(null)
  const [running, setRunning] = useState(false)
  const ask = useAsk()
  const nameRef = useRef(null)
  const editorRef = useRef(null)

  const refresh = () => {
    api('/api/agents').then((r) => setAgents(r.agents))
    api('/api/agents/trash').then((r) => setTrash(r.agents))
  }
  useEffect(() => { refresh() }, [])

  useEffect(() => {
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

  async function save() {
    try {
      // the whole definition goes back, including every field this view no
      // longer shows — omitting them would reset them to their defaults
      await api(`/api/agents/${selected}`, {
        method: 'PUT', body: JSON.stringify(agent) })
      setDirty(false)
      refresh()
      return true
    } catch (err) { notifyError(err); return false }   // e.g. 400: missing project
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

  // A run is detached server-side: this only starts it and holds its id for
  // Stop. It is deliberately not watchRun()-registered, so the completion
  // notice toasts; the transcript is on the Outputs tab.
  async function run(task, confirmPeak = false) {
    if (!task) {
      task = (await ask.prompt('Task', '', { confirmLabel: 'Run' }) || '').trim()
      if (!task) return
      if (dirty && !await save()) return
    }
    setRunning(true)
    try {
      await chatStream({ task, confirm_peak: confirmPeak }, (ev) => {
        if (ev.type === 'start') setRunId(ev.conversation_id)
        if (ev.type === 'error') notifyError(new Error(ev.message))
      }, `/api/agents/${agent.slug}/run`)
    } catch (err) {
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        setRunning(false)
        if (await ask.confirm('Peak pricing window — run anyway?',
                              { confirmLabel: 'Run' })) return run(task, true)
        return
      }
      notifyError(err)
    }
    setRunId(null)
    setRunning(false)
  }

  async function stop() {
    if (!runId) return
    try { await api(`/api/agents/runs/${runId}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }

  return (
    <>
      <aside>
        <form className="row agent-new" onSubmit={create}>
          <input ref={nameRef} className="grow" placeholder="new agent name"
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
              </button>
            </li>
          ))}
          {agents.length === 0 && <EmptyState as="li">none yet — press n</EmptyState>}
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
      </aside>
      <main ref={editorRef} className={agent ? 'editor-pane' : 'editor-pane split-idle'}>
        {!agent ? (
          <EmptyState pad>select an agent, or press <kbd>n</kbd> to create one</EmptyState>
        ) : (
          <div className="stack">
            <Toolbar variant="pane" title={agent.name}>
              <Button variant="ghost" danger onClick={del}>Delete</Button>
              {running
                ? <Button variant="ghost" disabled={!runId} onClick={stop}>Stop</Button>
                : <Button variant="ghost" onClick={() => run()}>Run</Button>}
              <SaveButton dirty={dirty} onSave={save} />
            </Toolbar>
            <Input label="name" value={agent.name}
                   onChange={(e) => patch({ name: e.target.value })} />
            <textarea className="md-editor" rows={16} spellCheck={false}
                      aria-label="prompt" value={agent.prompt}
                      onChange={(e) => patch({ prompt: e.target.value })} />
          </div>
        )}
      </main>
    </>
  )
}
