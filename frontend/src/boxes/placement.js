// Per-project "Runs in" (backend/vm/placement.py): pure helpers for the
// picker in RunsIn.jsx. Node-tested in __tests__/placement.test.mjs.
//
// A placement setting is {mode: 'profile'|'shared'|'own'|'join', runtime,
// image, mem_mb, box_id}. The server answers GET/PUT with
// {setting, effective, described, profile, boxes:[row], images:[{name}],
// budget, join_warning, warnings?}; a box row is
// {id, kind, owner, runtime, image, mem_mb, state, used_by:[slug], joined,
//  in_use, current}.

export const runtimeLabel = (rt) => (rt === 'docker' ? 'container' : 'VM')

// The boxes currently in use (running or chosen by some project), shared first.
export function inUseBoxes(boxes) {
  return (boxes || []).filter((b) => b.in_use)
}

// One searchable list over boxes and image variants. An image already shown
// as a running box is still offered: "a new box from this image" is a
// different choice from "this box". Query matches id, owner, image, runtime,
// state and the projects using a box.
export function pickerItems(boxes, images, query = '') {
  const q = String(query || '').trim().toLowerCase()
  const hit = (...xs) => !q || xs.some((x) => String(x ?? '').toLowerCase().includes(q))
  const out = []
  for (const b of boxes || []) {
    const words = [b.id, b.owner, b.image, runtimeLabel(b.runtime), b.state,
      ...(b.used_by || []), b.in_use ? 'in use' : '']
    if (hit(...words)) out.push({ type: 'box', key: `box:${b.id}`, box: b })
  }
  for (const im of images || []) {
    const running = (boxes || []).some((b) => b.image === im.name && b.state === 'running')
    if (hit(im.name, 'image', running ? 'running' : 'not running')) {
      out.push({ type: 'image', key: `image:${im.name}`, image: im, running })
    }
  }
  return out
}

// What picking a box means for project `slug`:
//   'current'  it already runs there: nothing to do
//   'shared'   the shared box: {mode:'shared'}
//   'own'      its own box: {mode:'own'} with that box's runtime/image
//   'ask'      another project's box: ask Share (join) or Separate (new own
//              box from the same image and runtime)
export function pickOutcome(box, slug, effective) {
  if (!box) return { kind: 'none' }
  if (effective?.box_id === box.id) return { kind: 'current' }
  if (box.id === 'shared' || box.kind === 'shared') return { kind: 'shared', body: { mode: 'shared' } }
  if (box.id === `p-${slug}`) {
    return { kind: 'own', body: { mode: 'own', runtime: box.runtime, image: box.image } }
  }
  const users = (box.used_by || []).filter((s) => s !== slug)
  return {
    kind: 'ask',
    users: users.length ? users : [box.owner].filter(Boolean),
    share: { mode: 'join', box_id: box.id },
    separate: separateBody(box),
  }
}

// "Separate: new box from the same image": own, the picked box's image and
// runtime, memory as given (blank = the profile's).
export function separateBody(box, mem) {
  const m = parseInt(mem, 10)
  return {
    mode: 'own', runtime: box.runtime || 'kvm', image: box.image || 'main',
    mem_mb: Number.isFinite(m) && m > 0 ? m : null,
  }
}

// [+ New box] / an image picked from the list.
export function newBoxBody({ runtime, image, mem }) {
  const m = parseInt(mem, 10)
  return {
    mode: 'own', runtime: runtime === 'docker' ? 'docker' : 'kvm', image: image || 'main',
    mem_mb: Number.isFinite(m) && m > 0 ? m : null,
  }
}

// Client-side check before a PUT (the server re-checks everything).
export function memError(mem) {
  if (mem === '' || mem == null) return null
  const m = Number(mem)
  if (!Number.isInteger(m)) return 'a whole number of MB'
  if (m < 256 || m > 65536) return '256 to 65536 MB'
  return null
}

// One line for where the project runs now.
export function effectiveText(eff, profileName) {
  if (!eff) return ''
  const from = eff.source !== 'profile' ? ''
    : profileName ? ` (default of the ${profileName} profile)` : ' (profile default)'
  if (eff.mode === 'shared') return `the shared box${from}`
  if (eff.mode === 'join') return `${eff.box_id}, ${eff.owner}'s box (shared with it)`
  return `its own ${runtimeLabel(eff.runtime)} ${eff.box_id} · image ${eff.image || 'main'}`
    + `${eff.mem_mb ? ` · ${eff.mem_mb} MB` : ''}${from}`
}
