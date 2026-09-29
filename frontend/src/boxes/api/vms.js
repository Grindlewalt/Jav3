// Boxes (docs/boxes-api-final.md section 1, WP1 + WP8 runtimes). The one
// module the VM manager, the profile form's runtime picker and the nav dot
// read boxes through.
import { enc, follow, get, missing, post } from '../http.js'
import { normRuntimes } from '../logic.js'

// GET /api/vm/boxes ->
//   {enabled, boxes:[row], budget:{ram_mb_used, ram_mb_cap, boxes, boxes_cap,
//    project_boxes, project_boxes_cap},
//    runtimes:{kvm:{available, reason}, docker:{available, reason, rootless,
//    userns, gvisor, seccomp, weak, warnings:[str]}}}
// Flag off: `boxes` is just the shared box. On a server without the route the
// shared guest's GET /api/vm/status is folded into the same shape (legacy).
export async function listBoxes() {
  try {
    const r = await get('/api/vm/boxes')
    return {
      enabled: !!r.enabled, boxes: r.boxes || [], budget: r.budget || null,
      runtimes: normRuntimes(r.runtimes), legacy: false, idle: r.idle || null,
    }
  } catch (e) {
    if (!missing(e)) throw e
    const s = await get('/api/vm/status')
    return {
      enabled: false, legacy: true, budget: null, boxes: [sharedFromStatus(s)], status: s,
      runtimes: normRuntimes(null),
    }
  }
}

function sharedFromStatus(s) {
  return {
    id: 'shared', kind: 'shared', project: null, cid: 3, runtime: 'kvm',
    state: s?.running ? 'running' : 'stopped',
    image: { variant: 'main', version: s?.image_version || null },
    mem_mb: s?.memory_mb || 768, rss_bytes: null, cpu_pct: null,
    uptime_s: s?.age_seconds ?? null, inflight: s?.inflight || 0,
    disk: { overlay_bytes: null, data_bytes: null }, net: {},
  }
}

// start/stop return the row; 409 = over a cap (detail says which), 404 an
// unknown id, 502 a boot failure. destroy: 400 without confirm.
export const startBox = (id) => post(`/api/vm/boxes/${enc(id)}/start`)
export const stopBox = (id) => post(`/api/vm/boxes/${enc(id)}/stop`)
export const destroyBox = (id, deleteData) =>
  post(`/api/vm/boxes/${enc(id)}/destroy`, { confirm: true, delete_data: !!deleteData })
// stop + start from a fresh overlay / container; returns the row
export const restartBox = (id) => post(`/api/vm/boxes/${enc(id)}/restart`)

// GET /api/vm/boxes/{id}/events -> {box_id, events:[{id, event, reason, actor,
// created_at (UTC), kind, project, runtime}]}, newest first. event: started |
// stopped | restarted | idle_stopped | wiped | nuked | destroyed | crashed | error
export const boxEvents = (id, limit = 50) =>
  get(`/api/vm/boxes/${enc(id)}/events?limit=${limit}`).then((r) => r.events || [])

// GET /api/vm/leftovers -> {items:[{id, type, name, why, cleanable, bytes?,
// detail}], cleanable, docker}: what boxes left behind that no box owns.
export const listLeftovers = (fresh = false) =>
  get(`/api/vm/leftovers${fresh ? '?fresh=true' : ''}`)
// removes only the cleanable ones, each re-identified first -> {removed,
// failed:[{id, error}], skipped, left}
export const cleanLeftovers = (ids = null) =>
  post('/api/vm/leftovers/clean', ids ? { confirm: true, ids } : { confirm: true })

// Live: topic `vm-boxes` on the shared stream: {type:"box_up"|"box_down",
// box:<static half of a row>} and {type:"box_event", box_id, event, ...}
// (the history). Refetch listBoxes on any.
export const followBoxes = (fn) => follow('vm-boxes', (ev) => {
  if (ev && (ev.type === 'box_up' || ev.type === 'box_down' || ev.type === 'box_event')) fn(ev)
})

// the shared guest's existing controls (vm_api.py), kept from the old VM chip
export const vmStatus = () => get('/api/vm/status')
export const nukeShared = () => post('/api/vm/nuke', { confirm: true })
export const rebuildBase = () => post('/api/vm/rebuild', { confirm: true })
