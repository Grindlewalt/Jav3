// The Desktop window's decisions, as pure functions (no React, no fetch), so a
// node test covers them (src/desktop/__tests__/logic.test.mjs).
//
// GET /api/vm/boxes/{id}/display (backend/vm/display_api.py) answers
//   {box_id, runtime, image, supported, reason, state: running|stopped,
//    session: off|running|stopped|unavailable|missing, viewers, need_mb, free_mb,
//    fits, geometry:{width,height}, control, agent, desk, guest, note}
// plus (P3) control: {holder: 'agent'|'operator', viewer, by, held_s} and
// agent: {active_age_s, turns: [conversation ids]}. The same two objects ride
// the shared stream (topic vm-boxes) as {type:'display', box_id, control|agent}.

export const SANDBOX_WARNING =
  'this is the sandbox, not your computer: don’t sign in to anything or type anything private here, the agent can read this screen'

// the agent counts as driving for this long after its last action
export const AGENT_ACTIVE_MS = 30000

// This window's id for its own noVNC session (?viewer= on the WebSocket): the
// server names the holder of control by it. 8-64 of [A-Za-z0-9_-].
export function newViewerId(rand = Math.random) {
  let s = ''
  while (s.length < 16) s += Math.floor(rand() * 36 ** 4).toString(36).padStart(4, '0')
  return `v-${s.slice(0, 16)}`
}

// 41 -> '00:41', 3725 -> '1:02:05'
export function mmss(total) {
  const t = Math.max(0, Math.floor(total || 0))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const p = (n) => String(n).padStart(2, '0')
  return h ? `${h}:${p(m)}:${p(t % 60)}` : `${p(m)}:${p(t % 60)}`
}

// Did the agent act on this desktop within AGENT_ACTIVE_MS? `agent` is the status
// object with `at` (ms) stamped when it was received; the age keeps growing after.
export function agentActive(agent, now) {
  if (!agent || agent.active_age_s == null) return false
  return agent.active_age_s * 1000 + Math.max(0, now - (agent.at || now)) < AGENT_ACTIVE_MS
}

// seconds the operator has held it, as of `now`
export function heldSeconds(ctl, now) {
  if (!ctl || ctl.holder !== 'operator') return 0
  return (ctl.held_s || 0) + Math.max(0, Math.floor((now - (ctl.at || now)) / 1000))
}

// What the window is in: 'you' (this window holds control), 'other' (another
// window does), 'agent' (the agent acted lately) or 'watching'.
export function viewOf(ctl, viewerId, active) {
  if (ctl && ctl.holder === 'operator') return ctl.viewer === viewerId ? 'you' : 'other'
  return active ? 'agent' : 'watching'
}

// the badge in the header
export function statusLabel(view, held) {
  if (view === 'you') return `YOU have control: agent paused ${mmss(held)}`
  if (view === 'other') return 'another window has control'
  if (view === 'agent') return '● agent driving'
  return 'watching'
}

// the line above the screen while the operator does not hold it
export function takeLine(view) {
  if (view === 'agent') return 'Agent is driving · click the screen to take over'
  if (view === 'other') return 'Another window has control of this desktop'
  return 'Click the screen to take over · the agent is paused while you have it'
}

// a click or key in the viewer takes control only from these states, and only
// once the screen is connected
export const canTake = (view, conn) => conn === 'watching' && (view === 'agent' || view === 'watching')

// [Stop] stops the agent's turn in this box: shown only while there is a turn to stop
export const stopTurns = (view, active, agent) =>
  (view === 'agent' && active && agent && Array.isArray(agent.turns) ? agent.turns : [])

// The first key of a take-over is consumed (it is not sent to the box). Tab must
// still move focus out of the window, and a bare modifier is not a decision.
const NOT_A_TAKE = new Set(['Tab', 'Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'Escape'])
export const takesOver = (key) => !NOT_A_TAKE.has(key)

// the boxes a window can look at: project boxes and the shared box. A Docker
// one is listed too, so that picking it says plainly that Docker has no desktop
// (the server's words) instead of looking like the project has no box.
export function candidateBoxes(rows) {
  return (rows || []).filter((b) => b && (b.kind === 'project' || b.kind === 'shared'))
}

export const boxOptionLabel = (b) => (b.runtime === 'docker' ? `${b.id} (docker)` : b.id)

// Which box the window watches: the one the operator chose (kept in the
// window's state), else the project's own p-<slug>, else a box that serves the
// project, else none.
export function pickBox(rows, slug, chosen) {
  const list = candidateBoxes(rows)
  if (chosen) {
    const b = list.find((x) => x.id === chosen)
    if (b) return b
  }
  if (!slug) return null
  return list.find((b) => b.id === `p-${slug}`)
    || list.find((b) => (b.projects || []).includes(slug))
    || null
}

// "needs 1424 MB, 1034 MB free"
export const needLine = (st) => `needs ${st.need_mb} MB, ${st.free_mb} MB free`

// The start button's words. A box that is already running adds only the
// screen, so there are no numbers to show then.
export function startLabel(st) {
  if (!st) return 'Start desktop'
  return st.need_mb > 0 ? `Start desktop · ${needLine(st)}` : 'Start desktop'
}

// Why Start cannot be pressed, or null.
export function startBlocked(st) {
  if (!st) return 'checking…'
  if (!st.supported) return st.reason || 'this box has no desktop'
  if (st.session === 'missing' || st.session === 'unavailable') return st.note
  if (st.need_mb > 0 && !st.fits) return `${needLine(st)}: stop another box first`
  return null
}

// What the body shows. `conn` is the noVNC connection: idle | connecting |
// watching | closed | lost.
export function panelMode(st, conn, err) {
  if (err) return 'error'
  if (!st) return 'loading'
  if (!st.supported) return 'unsupported'
  if (st.session === 'running') return conn === 'closed' || conn === 'lost' ? 'ended' : 'screen'
  return 'idle'
}

export function viewersText(n) {
  return n > 1 ? `${n} watching` : ''
}

// ws(s)://host/api/vm/boxes/<id>/display/ws?viewer=<this window's id>
export function displayWsUrl(loc, boxId, viewerId) {
  const proto = loc.protocol === 'https:' ? 'wss' : 'ws'
  const base = `${proto}://${loc.host}/api/vm/boxes/${encodeURIComponent(boxId)}/display/ws`
  return viewerId ? `${base}?viewer=${encodeURIComponent(viewerId)}` : base
}

// Is this vm-boxes stream event about the box (or about every box)?
export function eventTouches(ev, boxId) {
  if (!ev) return false
  if (ev.type === 'display' || ev.type === 'box_event') return ev.box_id === boxId
  if (ev.type === 'box_up' || ev.type === 'box_down') return ev.box?.id === boxId
  return false
}
