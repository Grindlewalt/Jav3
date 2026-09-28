// Per-project "Runs in" (backend/placement_api.py). Operator only.
//   GET /api/projects/{slug}/placement
//   PUT /api/projects/{slug}/placement {mode, runtime?, image?, mem_mb?, box_id?}
// Both answer the same overview (see ../placement.js); PUT adds `warnings`.
import { enc, get, put } from '../http.js'

export const getPlacement = (slug) => get(`/api/projects/${enc(slug)}/placement`)
export const setPlacement = (slug, body) => put(`/api/projects/${enc(slug)}/placement`, body)
