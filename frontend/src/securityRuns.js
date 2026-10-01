// The Security Queue as one card per run (backend/secruns.py): the pure parts.
// What counts as a card, what its header says, which buttons an event gets, and
// the sentences that go with them. The components (SecurityRuns.jsx) only draw.
//
// EVERYTHING that comes from an agent (a step's command, what it said it was
// doing, a file path, a program name) is plain text: nothing here builds markup.

export const UNTRUSTED = "the agent's words: untrusted"

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

// "2 need you · 47 filtered as normal work · 1 agent report": only what is there
export function countsParts(counts = {}) {
  const out = []
  if (counts.need > 0) out.push({ key: 'need', text: `${counts.need} need you` })
  if (counts.filtered > 0) {
    out.push({ key: 'filtered', text: `${counts.filtered} filtered as normal work`, show: true })
  }
  if (counts.reports > 0) {
    out.push({ key: 'reports', text: plural(counts.reports, 'agent report') })
  }
  return out
}

export const countsLine = (counts) => countsParts(counts).map((p) => p.text).join(' · ')

// the run's state, as one word for the header (a box or project group has none)
export function runState(run) {
  if (run?.running === true) return 'running'
  if (run?.running === false) return 'finished'
  return ''
}

// What the "Open chat" link points at: the conversation, and when the event
// names its step, that step (the chat scrolls to it and opens it)
export function chatHref(conversationId, stepId) {
  if (!conversationId) return null
  return stepId ? `/c/${conversationId}?step=${stepId}` : `/c/${conversationId}`
}

// the program an unexpected_process event names, as the baseline route wants it
export function programOf(ev) {
  const exe = ev?.detail?.exe
  if (typeof exe !== 'string' || !exe) return null
  return { exe, name: exe.split('/').filter(Boolean).pop() || exe, unit: ev.detail.unit || '' }
}

// A kind that always pings (critical) cannot be muted: the server refuses it
export const mutable = (ev) => ev?.tier !== 'critical' && ev?.severity !== 'critical'

// The title of one kind's line in a card
export function lineTitle(ev) {
  if (!ev) return ''
  if (ev.kind === 'unexpected_process') return 'New program outside the box baseline'
  if (ev.kind === 'proc_report_mismatch') return 'A connection no reported process owns'
  return ev.summary || ev.kind
}

// "(node, crashpad)": the distinct subjects of a kind line, if there is more than one thing
export function subjectsText(line) {
  const s = (line?.subjects || []).filter(Boolean)
  return s.length > 1 || (s.length === 1 && line.n > 1) ? `(${s.join(', ')})` : ''
}

// The buttons an event gets, in order. The first is its own primary action; Open
// chat / Stop agent appear when the event is tied to a run; Mute kind and
// Acknowledge close every row (Acknowledge is the fallback: always there).
//   ev    the event row (with `doing` from GET /runs)
//   ctx   { running, hasChat }
export function eventActions(ev, ctx = {}) {
  const out = []
  const d = ev?.detail && typeof ev.detail === 'object' ? ev.detail : {}
  if (ev.kind === 'unexpected_process') {
    const p = programOf(ev)
    if (p) out.push({ id: 'allow', label: `Allow ${p.name}`, tip: allowTip(p) })
  }
  if (ev.kind === 'write_flag' && d.path && !d.refused) {
    out.push({ id: 'board', label: 'Open diff', tip: 'the flagged code, the diff against git HEAD' })
    out.push({ id: 'revert', label: 'Revert file',
      tip: 'put the file back to the committed copy (or delete it if the agent created it)' })
  }
  if ((ev.kind === 'egress_anomaly' || ev.kind === 'host_cut') && d.host) {
    out.push({ id: 'uncut', label: 'Un-cut host', tip: `let ${d.host} through again` })
    out.push({ id: 'board', label: 'Open evidence', tip: 'the requests, the baseline, the policy' })
  }
  if (ctx.hasChat) {
    out.push({ id: 'chat', label: ev.doing?.step ? 'Open chat at step' : 'Open chat',
      tip: ev.doing?.step ? `chat ${ev.doing.conversation?.id}, step #${ev.doing.step.id}` : '' })
  }
  if (ctx.hasChat && ctx.running !== false) {
    out.push({ id: 'stop', label: 'Stop agent', tip: 'stop the agent that did this (just that one)' })
  }
  if ((ev.kind === 'unexpected_process' || ev.kind === 'proc_report_mismatch')
      && Number.isInteger(d.pid) && d.box_id) {
    out.push({ id: 'kill', label: 'Kill process',
      tip: 'stop this process, only if it still is the one the box reported' })
  }
  if (ev.kind !== 'write_flag' && ev.kind !== 'unexpected_process'
      && ev.kind !== 'egress_anomaly' && ev.kind !== 'host_cut') {
    out.push({ id: 'board', label: 'Inspect', tip: 'the evidence behind this alert' })
  }
  if (mutable(ev)) out.push({ id: 'mute', label: 'Mute kind', tip: 'stop this kind interrupting you' })
  out.push({ id: 'ack', label: ev.kind === 'harness_fault' ? 'Mark resolved' : 'Acknowledge',
    tip: 'Mark it seen. A repeat raises a new alert.' })
  return out
}

function allowTip(p) {
  return `Stop alerting on ${p.exe}${p.unit ? ` in ${p.unit}` : ''}, in every box`
}

// Which kinds "Mute kinds" would touch in a card: its need-you kinds that are not locked
export function kindsToMute(run) {
  const seen = []
  for (const line of run?.kinds || []) {
    if (line.tier !== 'critical' && line.severity !== 'critical' && !seen.includes(line.kind)) {
      seen.push(line.kind)
    }
  }
  return seen
}

export const MUTE_MODES = [
  { mode: 'badge', label: 'Badge only', tip: 'counts in Security, never pings' },
  { mode: 'record', label: 'Record only', tip: 'filed in the History, out of the Queue' },
]

export function muteAsk(kinds) {
  return {
    title: kinds.length === 1 ? `Mute ${kinds[0]}?` : `Mute ${kinds.length} kinds?`,
    body: `${kinds.join(', ')} will be filed in the History only: no ping, not in the Queue. `
      + 'You can change it in Settings, Alerts.',
    confirm: 'Mute',
  }
}

// Stop agent is one agent; Stop whole run asks first, with the server's count
export function stopRunAsk(r, title) {
  const n = r?.agents?.length || 0
  const plans = r?.plans?.length ? ` and the plan of ${r.plans.join(', ')}` : ''
  return {
    title: `Stop the whole run${title ? `: ${title}` : ''}?`,
    body: `${plural(n, 'agent')} running${plans} would be stopped. `
      + 'Work in progress is cut off where it is.',
    confirm: 'Stop the whole run',
  }
}

export const resolveAsk = (run) => ({
  title: 'Resolve this group?',
  body: `${plural(run.counts?.need || 0, 'alert')} and ${plural(run.counts?.reports || 0, 'agent report')} `
    + 'in this run will be marked seen.',
  confirm: 'Resolve group',
})

// A project's Workspace panel shows only its own cards
export const cardsFor = (runs, slug) => (slug
  ? (runs || []).filter((r) => r.project === slug) : (runs || []))

// total waiting in cards (the nav badge counts the same rows)
export function cardTotals(runs) {
  let need = 0
  let reports = 0
  for (const r of runs || []) { need += r.counts?.need || 0; reports += r.counts?.reports || 0 }
  return { need, reports, runs: (runs || []).length }
}

// "chat 500" -> the header's first words; the card title is built by the server.
// Short clock for a row: the time part of "2026-10-01 05:31:12"
export function clock(at) {
  const m = /(\d{2}:\d{2})(?::\d{2})?/.exec(String(at || ''))
  return m ? m[1] : ''
}
