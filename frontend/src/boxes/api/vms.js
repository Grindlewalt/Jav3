// Boxes (contract J(e), WP1). The one module the VM manager and the nav dot
// read boxes through.
import { enc, get, missing, post } from '../http.js'

// GET /api/vm/boxes. Before WP1's route exists (or on a backend without it)
// the shared guest's GET /api/vm/status is folded into the same shape, so the
// page and the nav dot work on today's server.
export async function listBoxes() {
  try {
    const r = await get('/api/vm/boxes')
    return { enabled: !!r.enabled, boxes: r.boxes || [], budget: r.budget || null, legacy: false }
  } catch (e) {
    if (!missing(e)) throw e
    const s = await get('/api/vm/status')
    return { enabled: false, legacy: true, budget: null, boxes: [sharedFromStatus(s)], status: s }
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

export const startBox = (id) => post(`/api/vm/boxes/${enc(id)}/start`)
export const stopBox = (id) => post(`/api/vm/boxes/${enc(id)}/stop`)
export const destroyBox = (id, deleteData) =>
  post(`/api/vm/boxes/${enc(id)}/destroy`, { confirm: true, delete_data: !!deleteData })

// the shared guest's existing controls (vm_api.py), kept from the old VM chip
export const vmStatus = () => get('/api/vm/status')
export const nukeShared = () => post('/api/vm/nuke', { confirm: true })
export const rebuildBase = () => post('/api/vm/rebuild', { confirm: true })
