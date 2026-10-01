import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api.js'
import { useAsk } from './ask.jsx'
import { sevClass, ts } from './format.js'
import { notify, notifyError } from './notify.js'
import { baselineAsk, faultText } from './securityCopy.js'
import {
  MUTE_MODES, UNTRUSTED, chatHref, clock, countsParts, eventActions, kindsToMute, lineTitle,
  muteAsk, programOf, resolveAsk, runState, stopRunAsk, subjectsText,
} from './securityRuns.js'

// The Security Queue, one card per RUN (backend/secruns.py, GET /api/security/runs).
// A card is a run's whole story: what waits on you, how many rows rules filed as
// normal work, the agent's own reports, and for each kind the step the agent was
// at, in its words, with the buttons that deal with it.
//
// "What the agent was doing" is the agent's own text. It is UNTRUSTED and always
// labelled so; every string here is a plain text node, nothing goes through <Md>.

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) })

export default function RunCards({ runs, onChanged, onOpenBoard, slug }) {
  return (
    <div className="srun-list">
      {runs.map((r) => (
        <RunCard key={r.key} run={r} onChanged={onChanged} onOpenBoard={onOpenBoard}
                 inProject={!!slug} />
      ))}
    </div>
  )
}

function RunCard({ run, onChanged, onOpenBoard, inProject }) {
  const ask = useAsk()
  const [busy, setBusy] = useState(false)
  const [filtered, setFiltered] = useState(null)     // the rows rules filed, once shown
  const [showFiltered, setShowFiltered] = useState(false)
  // the card carries each kind's newest event; "show more" asks for all of them
  const [full, setFull] = useState(null)
  const sev = sevClass(run.severity)
  const state = runState(run)
  const events = run.events || []
  const key = encodeURIComponent(run.key)

  async function guard(fn) {
    setBusy(true)
    try { await fn() } catch (e) { notifyError(e) }
    setBusy(false)
  }
  const changed = () => {
    window.dispatchEvent(new Event('jarvis-files-changed'))
    onChanged()
  }

  async function loadAll() {
    try { setFull((await api(`/api/security/runs/${key}`)).events || []) }
    catch { setFull(events) }
  }
  // the card changed while its events were open: read them again
  useEffect(() => { if (full !== null) loadAll() }, [run.newest_id, run.counts.need]) // eslint-disable-line

  async function toggleFiltered() {
    const next = !showFiltered
    setShowFiltered(next)
    if (next && filtered === null) {
      try { setFiltered((await api(`/api/security/runs/${key}`)).filtered || []) }
      catch { setFiltered([]) }
    }
  }

  const stopRun = () => guard(async () => {
    const first = events[0]
    if (!first) return
    const dry = await post(`/api/security/events/${first.id}/stop`, { scope: 'run', dry_run: true })
    if (!dry.agents?.length && !dry.plans?.length) { notify(dry.message || 'Nothing is running.'); return }
    const q = stopRunAsk(dry, run.title)
    if (!await ask.confirm(q.title, { body: q.body, confirmLabel: q.confirm, danger: true })) return
    const r = await post(`/api/security/events/${first.id}/stop`, { scope: 'run', confirm: true })
    notify(r.message || 'Stopped.', { life: 8 })
    changed()
  })

  const resolve = () => guard(async () => {
    if (run.counts.need > 0) {
      const q = resolveAsk(run)
      if (!await ask.confirm(q.title, { body: q.body, confirmLabel: q.confirm })) return
    }
    await post(`/api/security/runs/${key}/ack`)
    changed()
  })

  const muteAll = () => guard(async () => {
    const kinds = kindsToMute(run)
    if (!kinds.length) return
    const q = muteAsk(kinds)
    if (!await ask.confirm(q.title, { body: q.body, confirmLabel: q.confirm })) return
    await api('/api/notifications/settings', {
      method: 'PUT',
      body: JSON.stringify({ kinds: Object.fromEntries(kinds.map((k) => [k, 'record'])) }) })
    notify(`${kinds.join(', ')}: record only from now on.`, { life: 8 })
    changed()
  })

  const lines = [...(run.kinds || []), ...(run.report_kinds || [])]
  return (
    <section className={`srun sev-${sev}`}>
      <header className="srun-head">
        {run.project && <span className="tag">{run.project}</span>}
        <strong className="srun-title">{run.title}</strong>
        {state && <span className={`tag srun-state ${state}`}>{state}</span>}
        <span className="grow" />
        <span className="dim small" title={`last ${ts(run.last_at)} UTC`}>{clock(run.last_at)}</span>
      </header>
      <div className="srun-counts">
        {countsParts(run.counts).map((p, i) => (
          <span key={p.key}>
            {i > 0 && ' · '}
            {p.text}
            {p.show && (
              <> (<button type="button" className="srun-link" onClick={toggleFiltered}
                          aria-expanded={showFiltered}>
                {showFiltered ? 'hide' : 'show'}</button>)</>)}
          </span>
        ))}
      </div>
      {showFiltered && <FilteredRows rows={filtered} onOpen={onOpenBoard} />}
      {lines.map((line) => (
        <KindLine key={line.kind} line={line} run={run}
                  events={(full || events).filter((e) => e.kind === line.kind)}
                  onMore={() => { if (full === null) loadAll() }}
                  busy={busy} guard={guard} changed={changed} onOpenBoard={onOpenBoard}
                  inProject={inProject} />
      ))}
      <footer className="srun-foot">
        {run.running === true && (
          <button type="button" className="ghost danger" disabled={busy} onClick={stopRun}
                  title="stop every agent in this run (asks first)">Stop whole run</button>)}
        {kindsToMute(run).length > 1 && (
          <button type="button" className="ghost" disabled={busy} onClick={muteAll}
                  title="file these kinds in the History only">Mute these kinds</button>)}
        <span className="grow" />
        <button type="button" className="ghost" disabled={busy} onClick={resolve}
                title="mark everything waiting in this run as seen">Resolve group</button>
      </footer>
    </section>
  )
}

// The rows rules filed as normal work: one line each, with the reason
function FilteredRows({ rows, onOpen }) {
  if (rows === null) return <p className="dim small srun-filtered">Loading…</p>
  if (!rows.length) return <p className="dim small srun-filtered">Nothing recorded.</p>
  return (
    <ul className="srun-filtered">
      {rows.map((r) => (
        <li key={r.id}>
          <button type="button" className="srun-link" onClick={() => onOpen(r)}
                  title="open the evidence">{r.summary}</button>
          {r.count > 1 && <span className="dim small"> ×{r.count}</span>}
          {r.rule && <span className="dim small"> — {r.rule}</span>}
        </li>
      ))}
    </ul>
  )
}

// One kind in a card: its newest event in full, the rest behind "show N more"
function KindLine({ line, run, events, onMore, busy, guard, changed, onOpenBoard, inProject }) {
  const [more, setMore] = useState(false)
  if (!events.length) return null
  const [first, ...rest] = events
  const toggle = () => { setMore(!more); if (!more && line.n > events.length) onMore() }
  return (
    <div className="srun-kind">
      <EventBlock ev={first} line={line} run={run} busy={busy} guard={guard} changed={changed}
                  onOpenBoard={onOpenBoard} head inProject={inProject} />
      {line.n > 1 && (
        <button type="button" className="srun-link srun-more" onClick={toggle}
                aria-expanded={more}>
          {more ? 'show fewer' : `show ${line.n - 1} more of this kind`}</button>)}
      {more && rest.map((ev) => (
        <EventBlock key={ev.id} ev={ev} line={line} run={run} busy={busy} guard={guard}
                    changed={changed} onOpenBoard={onOpenBoard} inProject={inProject} />))}
      {more && line.n > events.length && <p className="dim small">Loading…</p>}
    </div>
  )
}

function EventBlock({ ev, line, run, busy, guard, changed, onOpenBoard, head }) {
  const ask = useAsk()
  const sev = sevClass(ev.severity)
  const cid = ev.doing?.conversation?.id || ev.conversation_id || ev.detail?.conversation_id || null
  const actions = eventActions(ev, { running: run.running, hasChat: !!cid })
  const d = ev.detail && typeof ev.detail === 'object' ? ev.detail : {}
  const title = ev.kind === 'harness_fault' ? faultText(ev.summary, d.tool) : lineTitle(ev)
  const sub = head ? subjectsText(line) : ''
  const proc = programOf(ev)

  const run1 = {
    allow: () => guard(async () => {
      const q = baselineAsk({ ...d, exe: proc.exe, unit: proc.unit }, 'program')
      if (!await ask.confirm(q.title, { body: q.body, confirmLabel: q.confirm })) return
      const r = await post(`/api/security/events/${ev.id}/baseline`, { scope: 'program' })
      notify(`Allowed. ${r.acknowledged} alert${r.acknowledged === 1 ? '' : 's'} cleared.`, { life: 8 })
      changed()
    }),
    board: () => onOpenBoard(ev),
    revert: () => guard(async () => {
      if (!await ask.confirm(`Put ${d.path} back?`, {
        body: 'It goes back to the copy git HEAD holds (or is deleted if the agent created it). '
          + 'Edits made since the last commit are lost. It is refused if the file changed '
          + 'after this alert.', confirmLabel: 'Revert file', danger: true })) return
      const r = await post(`/api/security/events/${ev.id}/revert`)
      notify(`${r.path}: ${r.action}.`, { life: 8 })
      changed()
    }),
    uncut: () => guard(async () => {
      if (!await ask.confirm(`Un-cut ${d.host}?`, {
        body: 'The block is lifted and the host is not re-judged for an hour. '
          + 'Look at the evidence first: an anomaly cut is a sign something may be wrong.',
        confirmLabel: 'Un-cut host' })) return
      await post(`/api/security/events/${ev.id}/uncut`)
      notify(`${d.host} is let through again.`, { life: 8 })
      changed()
    }),
    stop: () => guard(async () => {
      if (!await ask.confirm('Stop this agent?', {
        body: 'Just the agent that did this, not the rest of its run.',
        confirmLabel: 'Stop agent', danger: true })) return
      const r = await post(`/api/security/events/${ev.id}/stop`, { scope: 'agent' })
      notify(r.message || 'Stopped.', { life: 8 })
      changed()
    }),
    kill: () => guard(async () => {
      if (!await ask.confirm(`Stop ${proc?.name || 'this process'} (pid ${d.pid})?`, {
        body: 'It is only stopped if it still is the process the box reported. '
          + 'A pid that was reused is left alone.',
        confirmLabel: 'Kill process', danger: true })) return
      await post(`/api/security/events/${ev.id}/kill`)
      notify('Process stopped.', { life: 8 })
      changed()
    }),
    ack: () => guard(async () => {
      await post(`/api/security/events/${ev.id}/ack`)
      changed()
    }),
  }
  const mute = async (mode) => guard(async () => {
    await api('/api/notifications/settings', {
      method: 'PUT', body: JSON.stringify({ kinds: { [ev.kind]: mode } }) })
    notify(`${ev.kind}: ${mode === 'record' ? 'record only' : 'badge only'} from now on.`, { life: 8 })
    changed()
  })

  return (
    <div className={`srun-ev sev-${sev}`}>
      <div className="srun-evtop">
        <span className={`tag sev-${sev}-tag`}>{ev.severity}</span>
        <span className="srun-evtitle">{title}</span>
        {head && line.n > 1 && <span className="tag" title={`${line.n} events`}>×{line.n}</span>}
        {ev.count > 1 && <span className="tag" title="repeated while it waited">×{ev.count}</span>}
        {sub && <span className="mono small srun-subjects">{sub}</span>}
        {ev.actor === 'operator' && <span className="tag by-you">by you</span>}
      </div>
      {!head && d.path && <div className="mono small srun-subject">{d.path}</div>}
      {ev.doing && <Doing doing={ev.doing} />}
      <div className="srun-actions">
        {actions.map((a) => {
          if (a.id === 'chat') {
            return (
              <Link key="chat" className="ghost-link" to={chatHref(cid, ev.doing?.step?.id)}
                    title={a.tip}>{a.label}</Link>)
          }
          if (a.id === 'mute') {
            return (
              <details key="mute" className="srun-mute">
                <summary className="ghost" title={a.tip}>{a.label} ▾</summary>
                <div className="srun-mute-menu">
                  {MUTE_MODES.map((m) => (
                    <button key={m.mode} type="button" className="ghost" disabled={busy}
                            title={m.tip} onClick={() => mute(m.mode)}>{m.label}</button>))}
                </div>
              </details>)
          }
          return (
            <button key={a.id} type="button" className={`ghost${a.id === 'ack' ? ' srun-ack' : ''}`}
                    disabled={busy} title={a.tip} onClick={run1[a.id]}>{a.label}</button>)
        })}
      </div>
    </div>
  )
}

// The step and what the agent said it was doing, labelled as the agent's
function Doing({ doing }) {
  const step = doing.step
  const says = doing.says
  if (!step && !says) return null
  return (
    <div className="srun-doing">
      <div className="srun-doing-head dim small">
        What the agent was doing <span className="srun-untrusted">({UNTRUSTED})</span></div>
      {step && (
        <div className="srun-step mono small">
          step #{step.id} {step.tool}{step.command ? `: ${step.command}` : ''}
          {step.match === 'nearest' && (
            <span className="dim"> (nearest earlier step: the event names none)</span>)}
        </div>)}
      {says && (
        <div className="srun-says small">
          {says.source === 'narration' ? 'says' : says.source === 'todo' ? 'its todo list says'
            : 'its chat is titled'}: “{says.text}”</div>)}
    </div>
  )
}
