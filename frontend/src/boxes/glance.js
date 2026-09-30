// Pure helpers for the Boxes page's "at a glance" columns: when a box stops,
// what it is doing, and its history. Same words as the terminal client's
// /vms rows (clients/jav3cli/jav3, _box_timer / _box_event_line).

// 45s, 4m, 1h05m
export function mins(s) {
  const n = Math.max(0, Math.floor(Number(s) || 0))
  if (n < 60) return `${n}s`
  if (n < 3600) return `${Math.floor(n / 60)}m`
  return `${Math.floor(n / 3600)}h${String(Math.floor((n % 3600) / 60)).padStart(2, '0')}m`
}

// When the reaper acts on this box, from the server-computed fields
// (idle_s, stop_action, stop_after_s, stops_in_s):
//   project  "idle 4m (stops at 10m)"   stopped and released after that idle
//   shared   "scrub after 11m"          rebooted fresh (vm_idle_scrub_seconds)
// {text, title, tone} or null (running a turn, stopped, or no idle stop).
export function idleTimer(b) {
  if (!b || b.state !== 'running' || b.idle_s == null || !b.stop_after_s) return null
  if (b.stop_action === 'stop') {
    const left = b.stops_in_s ?? Math.max(0, b.stop_after_s - b.idle_s)
    return {
      text: `idle ${mins(b.idle_s)} (stops at ${mins(b.stop_after_s)})`,
      title: `stopped and released after ${mins(b.stop_after_s)} idle — in about ${mins(left)}`,
      tone: left < 60 ? 'pending' : undefined,
    }
  }
  if (b.stop_action === 'scrub') {
    const left = b.stops_in_s ?? Math.max(0, b.stop_after_s - b.idle_s)
    return {
      text: `scrub after ${mins(left)}`,
      title: `idle ${mins(b.idle_s)}; rebooted fresh after ${mins(b.stop_after_s)} idle`,
      tone: left < 60 ? 'pending' : undefined,
    }
  }
  return null
}

// The policy line under the budget: when idle boxes are stopped or scrubbed.
export function idlePolicy(idle, enabled) {
  if (!idle) return ''
  const bits = []
  if (enabled && idle.project_stop_s) bits.push(`project boxes stop after ${mins(idle.project_stop_s)} idle`)
  bits.push(idle.shared_scrub_s
    ? `the shared box is scrubbed after ${mins(idle.shared_scrub_s)} idle`
    : 'the shared box is never scrubbed')
  return bits.join(' · ')
}

// The turns running in a box now, each as {head, title, tool, detail, project}.
export function doingNow(b) {
  return (Array.isArray(b?.now) ? b.now : []).map((t) => ({
    key: t.op_id || String(t.conversation_id),
    head: t.conversation_id ? `#${t.conversation_id}` : 'a turn',
    conversationId: t.conversation_id || null,
    title: t.title || '',
    project: t.project || '',
    tool: t.tool?.name || '',
    detail: t.tool?.detail || '',
    toolSince: t.tool?.since || null,
  }))
}

// One word for the row when nothing is running in it.
export function activityWord(b) {
  const a = b?.activity
  if (a === 'failed') return 'failed'
  if (a === 'starting') return 'starting'
  if (b?.state !== 'running') return 'stopped'
  return a === 'busy' ? 'busy' : 'idle'
}

export const EVENT_TONE = {
  started: 'done', restarted: 'done', stopped: undefined, idle_stopped: undefined,
  wiped: 'pending', nuked: 'pending', destroyed: 'pending', crashed: 'error', error: 'error',
}

export const eventWord = (e) => String(e?.event || '?').replace(/_/g, ' ')

// "09-28 14:03" in local time, from epoch seconds or a UTC SQLite datetime.
export function localTime(v) {
  if (v == null || v === '') return ''
  let t
  if (typeof v === 'number') t = v * 1000
  else {
    const iso = String(v).replace(' ', 'T')
    t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  }
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// Leftovers grouped for the card's summary: "2 containers, 1 box directory".
const LEFT_NAMES = {
  qemu: ['QEMU process', 'QEMU processes'], container: ['container', 'containers'],
  box_dir: ['box directory', 'box directories'], sock_dir: ['socket directory', 'socket directories'],
  overlay: ['stale overlay', 'stale overlays'], tap: ['network interface', 'network interfaces'],
}
export function leftoverSummary(items) {
  const n = {}
  for (const i of items || []) n[i.type] = (n[i.type] || 0) + 1
  return Object.entries(n).map(([k, c]) => {
    const [one, many] = LEFT_NAMES[k] || [k, k]
    return `${c} ${c === 1 ? one : many}`
  }).join(', ')
}
