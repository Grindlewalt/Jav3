// /persist retirement (docs/boxes-api-final.md section 4, WP3; decision 0.3) and the
// project list the boxes pages share.
import { enc, get, post, put } from '../http.js'

export async function listProjects() {
  const r = await get('/api/projects')
  return r.projects || []
}

// {slug, approved, approved_at, enabled, mount, disk:{exists, bytes_used,
// cap_bytes}, attached, read_only, retired: true, imported_at, delete_after,
// import: {state: "pending"|"done"|"failed", error?} | null}
export const getPersist = (slug) => get(`/api/projects/${enc(slug)}/persist`)
export const importPersist = (slug) =>
  post(`/api/projects/${enc(slug)}/persist/import`, { confirm: true })
// explicit delete: revoke + delete the disk (the one PUT still accepted;
// {approved: true} is a 409 — approvals are frozen)
export const deletePersist = (slug) =>
  put(`/api/projects/${enc(slug)}/persist`, { approved: false, delete_disk: true })
