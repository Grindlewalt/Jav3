// /persist retirement (contract J(a), WP3; operator decision 0.3) and the
// project list the boxes pages share.
import { enc, get, post, put } from '../http.js'

export async function listProjects() {
  const r = await get('/api/projects')
  return r.projects || []
}

// The existing view: {approved, approved_at, enabled, mount, disk:{exists,
// bytes_used, cap_bytes}, attached} — plus, once WP3 lands, imported_at and
// delete_after (persist_imported_at / persist_delete_after also read).
export const getPersist = (slug) => get(`/api/projects/${enc(slug)}/persist`)
export const importPersist = (slug) =>
  post(`/api/projects/${enc(slug)}/persist/import`, { confirm: true })
// explicit delete: revoke + delete the disk (the one PUT still accepted)
export const deletePersist = (slug) =>
  put(`/api/projects/${enc(slug)}/persist`, { approved: false, delete_disk: true })
