// The Desktop window's decisions, as pure functions (no React, no fetch), so a
// node test covers them (src/desktop/__tests__/logic.test.mjs).
//
// GET /api/vm/boxes/{id}/display (backend/vm/display_api.py) answers
//   {box_id, runtime, image, supported, reason, state: running|stopped,
//    session: off|running|stopped|unavailable|missing, viewers, need_mb, free_mb,
//    fits, geometry:{width,height}, watch_only, guest, note}

export const WATCH_LABEL = 'watching · view only'
export const SANDBOX_WARNING =
  'this is the sandbox, not your computer: don’t sign in to anything here, the agent can read this screen'

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

// ws(s)://host/api/vm/boxes/<id>/display/ws
export function displayWsUrl(loc, boxId) {
  const proto = loc.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${loc.host}/api/vm/boxes/${encodeURIComponent(boxId)}/display/ws`
}

// Is this vm-boxes stream event about the box (or about every box)?
export function eventTouches(ev, boxId) {
  if (!ev) return false
  if (ev.type === 'display' || ev.type === 'box_event') return ev.box_id === boxId
  if (ev.type === 'box_up' || ev.type === 'box_down') return ev.box?.id === boxId
  return false
}
