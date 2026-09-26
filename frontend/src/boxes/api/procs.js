// The persistent-process view (docs/boxes-api-final.md section 6, WP4).
import { follow, get, qs } from '../http.js'

// GET /api/vm/processes[?box=<id>] -> {enabled, boxes:[row]}. A row:
//   {box_id, kind, project, reported_at, stale, error, baseline: "image"|"builtin",
//    truncated, totals|null, orphan_conns:[conn], tree:[...]}
// Flag off: {enabled:false, boxes:[]}. An unknown ?box= is a 404.
export async function listProcesses(box) {
  const r = await get(`/api/vm/processes${qs({ box })}`)
  return { enabled: r.enabled !== false, boxes: r.boxes || [] }
}

// Live: topic `procs` on the ONE shared /api/events stream, one box per
// event (stream_open, box_procs, box_procs_changed, box_gone): fold them with
// logic.mergeProcs and fetch what logic.procsRefetch names.
export const followProcs = (fn) => follow('procs', fn)
