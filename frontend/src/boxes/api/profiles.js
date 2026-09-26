// Security profiles (docs/boxes-api-final.md section 2, WP2). POST needs
// service_placement + box_runtime (422 without); DELETE is 409 for a builtin
// or a profile still in use; builtins may be edited but not renamed.
import { del, enc, get, post, put } from '../http.js'

// -> [{id, name, builtin, default_verdict, network_off, allow_hosts,
//     deny_hosts, secrets, auto_handle, separate_box, box_image, box_mem_mb,
//     box_runtime, allow_services, allow_package_requests, service_placement,
//     projects:[slugs]}]
export async function listProfiles() {
  const r = await get('/api/profiles')
  return r.profiles || []
}
export const createProfile = (body) => post('/api/profiles', body)
export const updateProfile = (id, body) => put(`/api/profiles/${enc(id)}`, body)
export const deleteProfile = (id) => del(`/api/profiles/${enc(id)}`)
// -> {ok, project, profile:{id, name}}; 404 unknown project, 409 __image_build__
export const assignProfile = (slug, profileId) =>
  put(`/api/projects/${enc(slug)}/profile`, { profile_id: profileId })

// secret NAMES only (GET /api/secrets never returns values)
export async function secretNames() {
  const r = await get('/api/secrets')
  const list = Array.isArray(r) ? r : (r.secrets || [])
  return list.map((s) => (typeof s === 'string' ? s : s.name)).filter(Boolean)
}
