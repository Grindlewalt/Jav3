// The persistent-process view (contract J(b), WP4).
import { follow, get, qs } from '../http.js'

// -> [{box_id, kind, project, reported_at, stale, tree:[...]}]
export async function listProcesses(box) {
  const r = await get(`/api/vm/processes${qs({ box })}`)
  return r.boxes || []
}

// Live: topic `procs` on the shared /api/events stream. The event shape is
// WP4's; logic.mergeProcs accepts a whole snapshot ({boxes}) or one box
// ({box}, or a bare box row with box_id).
export const followProcs = (fn) => follow('procs', fn)
