import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { Button, EmptyState, Input, Menu, MenuItem, MenuSep, SaveButton, Select, Tag } from './components/index.js'
import { notifyError } from './notify.js'

// The explicit orchestrator's checklist: dump -> plan -> run. The list is the
// project's .plan.json read back through /api/projects/{slug}/plan; every edit
// here is a normal edit of that file, which the runner honours on its next
// tick, so the panel stays usable while a run is live. Live item state rides
// the head's run stream (plan_item events); job_final refetches.
const TONE = { todo: 'pending', running: 'running', blocked: 'untrusted',
               done: 'done', failed: 'error', skipped: undefined }
const PLAN_TONE = { draft: undefined, running: 'running', done: 'done',
                    failed: 'error', stopped: 'pending' }

export default function PlanPanel({ slug, state, setState }) {
  const [plan, setPlan] = useState(null)
  const [running, setRunning] = useState(false)
  const [agents, setAgents] = useState([])
  const [busy, setBusy] = useState(false)
  const [newTitle, setNewTitle] = useState('')
  const [peakAsk, setPeakAsk] = useState(null)   // 'plan' | 'run'; in-page, iOS eats confirm()
  const dump = state.dump || ''

  const take = (r) => { setPlan(r.plan); setRunning(r.running) }
  const load = () => api(`/api/projects/${slug}/plan`).then(take).catch(notifyError)
  useEffect(() => { load() }, [slug]) // eslint-disable-line
  useEffect(() => { api('/api/agents').then((r) => setAgents(r.agents || [])).catch(() => {}) }, [])

  useEffect(() => {
    if (!plan?.root_id || !running) return
    const es = new EventSource(`/api/runs/${plan.root_id}/stream`)
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data)
      if (ev.type === 'plan_item') {
        const { type, job_id, ...it } = ev
        setPlan((p) => p ? { ...p, items: p.items.map((x) => x.id === it.id ? { ...x, ...it } : x) } : p)
      }
      if (ev.type === 'job_final') { es.close(); load() }
    }
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) load() }
    return () => es.close()
  }, [plan?.root_id, running]) // eslint-disable-line

  async function call(path, options, onPeak) {
    setBusy(true)
    try {
      take(await api(`/api/projects/${slug}/plan${path}`, options))
      return true
    } catch (err) {
      if (err.status === 409 && err.detail === 'peak_confirmation_required' && onPeak) setPeakAsk(onPeak)
      else notifyError(err)
      return false
    } finally { setBusy(false) }
  }
  const post = (path, body, onPeak) =>
    call(path, { method: 'POST', body: JSON.stringify(body || {}) }, onPeak)
  const patch = (id, body) =>
    call(`/items/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
  const remove = (id) => call(`/items/${id}`, { method: 'DELETE' })

  const makePlan = (confirm = false) => post('', { dump, confirm_peak: confirm }, 'plan')
  const run = (confirm = false) => post('/run', { confirm_peak: confirm }, 'run')
  const stop = () => post('/stop')
  const todo = plan?.items.filter((it) => it.status === 'todo').length || 0

  return (
    <div className="pane-col">
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (dump.trim()) makePlan() }}>
        <Input textarea className="grow plan-dump" rows={3} value={dump}
               aria-label="dump" placeholder="dump the ask here — a spec, notes, a list…"
               onChange={(e) => setState({ dump: e.target.value })} />
        <Button type="submit" disabled={busy || running || !dump.trim()}>Plan</Button>
      </form>
      {peakAsk && (
        <div className="peak-ask compact" role="alertdialog" aria-label="peak pricing confirmation">
          <span className="grow">Peak pricing right now — this costs 2×.</span>
          <Button variant="ghost" onClick={() => setPeakAsk(null)}>Cancel</Button>
          <Button onClick={() => { const w = peakAsk; setPeakAsk(null); (w === 'run' ? run : makePlan)(true) }}>
            {peakAsk === 'run' ? 'Run anyway' : 'Plan anyway'}
          </Button>
        </div>
      )}
      {!plan && <EmptyState pad>no plan yet — dump the ask above and press Plan</EmptyState>}
      {plan && (
        <>
          <div className="row plan-head">
            <strong className="grow ellipsis" title={plan.title}>{plan.title || 'Plan'}</strong>
            <Tag tone={PLAN_TONE[plan.status]}>{plan.status}</Tag>
            {running
              ? <Button variant="ghost" onClick={stop} disabled={busy}>Stop</Button>
              : <Button onClick={() => run()} disabled={busy || !todo}>Run</Button>}
          </div>
          <ul className="plan-list grow-scroll">
            {plan.items.map((it, i) => (
              <PlanItem key={it.id} it={it} index={i} count={plan.items.length}
                        agents={agents} busy={busy}
                        onPatch={(body) => patch(it.id, body)}
                        onDelete={() => remove(it.id)} />
            ))}
            {plan.items.length === 0 && <EmptyState as="li">no items — add one below</EmptyState>}
          </ul>
          <form className="row" onSubmit={async (e) => {
            e.preventDefault()
            if (!newTitle.trim()) return
            if (await post('/items', { title: newTitle })) setNewTitle('')
          }}>
            <Input className="grow" placeholder="add an item…" value={newTitle}
                   aria-label="new item title" onChange={(e) => setNewTitle(e.target.value)} />
            <Button type="submit" variant="ghost" disabled={busy || !newTitle.trim()}>Add</Button>
          </form>
        </>
      )}
    </div>
  )
}

function PlanItem({ it, index, count, agents, busy, onPatch, onDelete }) {
  const [open, setOpen] = useState(false)
  const [menu, setMenu] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [title, setTitle] = useState(it.title)
  const [brief, setBrief] = useState(it.brief)
  const [assignee, setAssignee] = useState(it.assignee || '')
  const titleRef = useRef(null)
  useEffect(() => { setTitle(it.title); setBrief(it.brief); setAssignee(it.assignee || '') }, [it.title, it.brief, it.assignee])
  useEffect(() => { if (renaming) titleRef.current?.select() }, [renaming])

  const dirty = brief !== it.brief || assignee !== (it.assignee || '')
  const commitTitle = () => {
    setRenaming(false)
    if (title.trim() && title.trim() !== it.title) onPatch({ title: title.trim() })
    else setTitle(it.title)
  }
  const act = (fn) => { setMenu(false); fn() }

  return (
    <li className={`plan-item ${it.status}`}>
      <div className="plan-row">
        <Tag tone={TONE[it.status]}>{it.status}</Tag>
        <span className="dim small">{it.id}</span>
        {renaming
          ? <input ref={titleRef} className="grow" value={title}
                   onChange={(e) => setTitle(e.target.value)} onBlur={commitTitle}
                   onKeyDown={(e) => { if (e.key === 'Enter') commitTitle(); if (e.key === 'Escape') { setTitle(it.title); setRenaming(false) } }} />
          : <span className="plan-title ellipsis" title={it.title}
                  onClick={() => setOpen((o) => !o)}>{it.title}</span>}
        {it.assignee && <span className="dim small">@{it.assignee}</span>}
        {it.depends_on.length > 0 && <span className="dim small">after {it.depends_on.join(', ')}</span>}
        {it.attempts > 1 && <span className="dim small" title="attempts">×{it.attempts}</span>}
        <Menu floating open={menu} onClose={() => setMenu(false)} label={`item ${it.id} actions`}
              trigger={
                <Button variant="icon" className="win-btn" aria-haspopup="menu" aria-expanded={menu}
                        disabled={busy} onClick={() => setMenu((m) => !m)}>⋯</Button>
              }>
          <MenuItem onClick={() => act(() => setRenaming(true))}>Rename</MenuItem>
          <MenuItem disabled={index === 0} onClick={() => act(() => onPatch({ position: index - 1 }))}>Move up</MenuItem>
          <MenuItem disabled={index >= count - 1} onClick={() => act(() => onPatch({ position: index + 1 }))}>Move down</MenuItem>
          <MenuSep />
          <MenuItem disabled={it.status === 'done'} onClick={() => act(() => onPatch({ status: 'done' }))}>Mark done</MenuItem>
          <MenuItem disabled={it.status === 'skipped'} onClick={() => act(() => onPatch({ status: 'skipped' }))}>Skip</MenuItem>
          <MenuItem disabled={it.status === 'todo'} onClick={() => act(() => onPatch({ status: 'todo' }))}>Reset to to-do</MenuItem>
          <MenuSep />
          <MenuItem danger onClick={() => act(onDelete)}>Delete</MenuItem>
        </Menu>
      </div>
      {open && (
        <div className="plan-detail">
          <Input textarea rows={3} label="brief" value={brief}
                 onChange={(e) => setBrief(e.target.value)} />
          <div className="row">
            <Select label="assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)}
                    options={[{ value: '', label: 'general worker' },
                              ...agents.map((a) => ({ value: a.slug, label: a.name }))]} />
            <SaveButton dirty={dirty} disabled={busy}
                        onSave={() => onPatch({ brief, assignee })} />
          </div>
          {it.result_summary && (
            <div className="small"><span className="dim">result: </span>{it.result_summary}</div>
          )}
          {it.last_error && it.status !== 'done' && (
            <div className="small error">{it.last_error}</div>
          )}
          {it.notes?.length > 0 && (
            <div className="small dim">{it.notes.length} note{it.notes.length === 1 ? '' : 's'} from teammates</div>
          )}
        </div>
      )}
    </li>
  )
}
