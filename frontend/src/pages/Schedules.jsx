import { useEffect, useState } from 'react'
import { api } from '../api.js'

// Heartbeats: "run X every day at 8am" / "every 6 hours". A schedule runs
// either a defined agent or a plain Jarvis prompt, headless, in an optional
// project's context. Peak pricing is auto-confirmed for scheduled runs.
const BLANK = {
  name: '', kind: 'jarvis', agent_slug: '', project_slug: '',
  task: '', cadence_kind: 'daily', daily_at: '09:00', interval_minutes: 360,
}

export default function Schedules() {
  const [schedules, setSchedules] = useState([])
  const [agents, setAgents] = useState([])
  const [projects, setProjects] = useState([])
  const [form, setForm] = useState(BLANK)
  const [busy, setBusy] = useState(null)
  const [editing, setEditing] = useState(null)   // schedule id being edited

  const refresh = () => api('/api/schedules').then((r) => setSchedules(r.schedules))
  useEffect(() => {
    refresh()
    api('/api/agents').then((r) => setAgents(r.agents))
    api('/api/projects').then((r) => setProjects(r.projects))
  }, [])

  const set = (p) => setForm((f) => ({ ...f, ...p }))

  async function save(e) {
    e.preventDefault()
    if (!form.name.trim() || !form.task.trim()) return
    if (form.kind === 'agent' && !form.agent_slug) { window.alert('pick an agent'); return }
    const body = JSON.stringify({
      ...form,
      interval_minutes: Number(form.interval_minutes) || 360,
      agent_slug: form.kind === 'agent' ? form.agent_slug : null,
      project_slug: form.project_slug || null,
    })
    try {
      if (editing) await api(`/api/schedules/${editing}`, { method: 'PUT', body })
      else await api('/api/schedules', { method: 'POST', body })
      cancelEdit()
      refresh()
    } catch (err) { window.alert(err.detail || String(err)) }
  }

  function openEdit(s) {
    setEditing(s.id)
    setForm({
      name: s.name, kind: s.kind, agent_slug: s.agent_slug || '',
      project_slug: s.project_slug || '', task: s.task,
      cadence_kind: s.cadence_kind, daily_at: s.daily_at || '09:00',
      interval_minutes: s.interval_minutes || 360,
    })
  }

  function cancelEdit() {
    setEditing(null)
    setForm(BLANK)
  }

  async function toggle(s) {
    await api(`/api/schedules/${s.id}?enabled=${!s.enabled}`, { method: 'PATCH' })
    refresh()
  }
  async function del(s) {
    if (!window.confirm(`delete schedule "${s.name}"?`)) return
    await api(`/api/schedules/${s.id}`, { method: 'DELETE' })
    refresh()
  }
  async function runNow(s) {
    setBusy(s.id)
    try { await api(`/api/schedules/${s.id}/run-now`, { method: 'POST' }) }
    catch (err) { window.alert(err.detail || String(err)) }
    setBusy(null)
    refresh()
  }

  const cadence = (s) => s.cadence_kind === 'daily'
    ? `daily at ${s.daily_at}`
    : `every ${s.interval_minutes} min`

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">{editing ? `Edit schedule #${editing}` : 'New schedule'}</div>
        <form className="sched-form" onSubmit={save}>
          <input placeholder="name (e.g. morning briefing)" value={form.name}
                 onChange={(e) => set({ name: e.target.value })} />
          <label className="mini">what runs
            <select value={form.kind} onChange={(e) => set({ kind: e.target.value })}>
              <option value="jarvis">Jarvis (main)</option>
              <option value="agent">an agent</option>
            </select>
          </label>
          {form.kind === 'agent' && (
            <label className="mini">agent
              <select value={form.agent_slug} onChange={(e) => set({ agent_slug: e.target.value })}>
                <option value="">— pick —</option>
                {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
              </select>
            </label>
          )}
          <label className="mini">project context (optional)
            <select value={form.project_slug} onChange={(e) => set({ project_slug: e.target.value })}>
              <option value="">— none —</option>
              {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
            </select>
          </label>
          <textarea rows={3} placeholder="task — what should it do?" value={form.task}
                    onChange={(e) => set({ task: e.target.value })} />
          <label className="mini">cadence
            <select value={form.cadence_kind} onChange={(e) => set({ cadence_kind: e.target.value })}>
              <option value="daily">daily at a time</option>
              <option value="interval">every N minutes</option>
            </select>
          </label>
          {form.cadence_kind === 'daily'
            ? <input type="time" value={form.daily_at} onChange={(e) => set({ daily_at: e.target.value })} />
            : <input type="number" min="15" value={form.interval_minutes}
                     onChange={(e) => set({ interval_minutes: e.target.value })} />}
          <button type="submit">{editing ? 'save changes' : '+ create'}</button>
          {editing && <button type="button" className="ghost" onClick={cancelEdit}>cancel</button>}
        </form>
      </aside>
      <main className="editor-pane">
        <div className="side-title">Schedules</div>
        {schedules.length === 0 && <p className="dim">none yet — set one up on the left</p>}
        <ul className="sched-list">
          {schedules.map((s) => (
            <li key={s.id} className={s.enabled ? '' : 'off'}>
              <div className="sched-head">
                <span className="grow"><strong>{s.name}</strong>
                  <span className="tag">{s.kind === 'agent' ? s.agent_slug : 'jarvis'}</span>
                  {s.project_slug && <span className="tag">{s.project_slug}</span>}
                </span>
                <button className="ghost" disabled={busy === s.id}
                        onClick={() => runNow(s)}>{busy === s.id ? '…' : 'run now'}</button>
                <button className="ghost" onClick={() => openEdit(s)}>edit</button>
                <button className="ghost" onClick={() => toggle(s)}>
                  {s.enabled ? 'pause' : 'resume'}</button>
                <button className="ghost danger" onClick={() => del(s)}>delete</button>
              </div>
              <div className="dim small">{s.task}</div>
              <div className="dim small">{cadence(s)} · next {s.next_run?.replace('T', ' ')}
                {s.last_run && ` · last ${s.last_run.replace('T', ' ')}`}</div>
              {s.last_result && <pre className="sched-result">{s.last_result}</pre>}
            </li>
          ))}
        </ul>
      </main>
    </div>
  )
}
