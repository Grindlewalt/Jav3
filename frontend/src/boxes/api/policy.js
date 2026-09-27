// Per-project egress allow/deny and the standing allowlist
// (docs/boxes-api-final.md section 3, WP2).
import { call, enc, get, post, put } from '../http.js'

// -> {slug, profile:{id, name, default, network_off, builtin}, project_allow,
//     project_deny, effective_allow, effective_deny, source}
// `__image_build__` answers the builders' fixed policy (source "fixed"),
// which cannot be edited.
export async function getPolicy(slug) {
  const p = await get(`/api/egress/policy/${enc(slug)}`)
  return {
    slug: p.slug || slug,
    profile: p.profile || null,
    project_allow: p.project_allow || [],
    project_deny: p.project_deny || [],
    effective_allow: p.effective_allow || [],
    effective_deny: p.effective_deny || [],
    fixed: p.source === 'fixed',
  }
}
// Replaces the project's OWN lists; a list left undefined is left as it is.
// Refused for __general__ (edit the Default profile) and __image_build__.
export const putPolicy = (slug, allow, deny) => {
  const body = {}
  if (allow !== undefined) body.allow = allow
  if (deny !== undefined) body.deny = deny
  return put(`/api/egress/policy/${enc(slug)}`, body)
}
// Move a host from the project's own list onto a profile's, in one call.
// profileId null = the project's own profile.
export const promoteToProfile = (slug, host, { profileId = null, list = 'allow' } = {}) =>
  post(`/api/egress/policy/${enc(slug)}/promote`,
    { host, profile_id: profileId, list: list === 'deny' ? 'deny' : 'allow' })

// GET /api/egress/allowlist -> [group], project groups first, then profiles:
//   {project: slug | "__general__" | "profile:<id>", kind: "project"|"profile",
//    profile:{id, name, default}, entries:[{host, source, id?, rule?, reason?,
//    created_at?, expires_at?}], deny:[hosts], projects?:[slugs]}
export async function allowlist() {
  const r = await get('/api/egress/allowlist')
  return r.groups || []
}
// {project (the group's key), host, id? (an auto entry), list: "allow"|"deny"}
export const revokeAllow = ({ project, host, id, list = 'allow' }) =>
  post('/api/egress/allowlist/revoke', {
    project: project || '', host: host || '', list: list === 'deny' ? 'deny' : 'allow',
    ...(id != null ? { id } : {}),
  })
export const promoteAuto = (id) => post(`/api/egress/auto/${enc(id)}/promote`)

// The waiting queue. An UNATTRIBUTED row (shared box, no turn) needs the
// operator to name the project whose list it goes on: {project}; without it
// the host answers 409.
export const approvePending = (id, project = null) =>
  post(`/api/egress/pending/${enc(id)}/approve`, project ? { project } : {})
export const rejectPending = (id) => call(`/api/egress/pending/${enc(id)}/reject`, { method: 'POST' })
// Allow a host that is not waiting (an auto-deny). -> {ok, host, added_to};
// a project is required (409 {"detail":"needs_project"} otherwise).
export const allowHost = (project, host) => post('/api/egress/allow', { project, host })

// Per-project LAN access (backend/lanaccess.py). OFF by default.
// -> {slug, enabled, allow:[entries], host_ips:[never reachable]}
export async function getLan(slug) {
  const r = await get(`/api/egress/lan/${enc(slug)}`)
  return { enabled: !!r.enabled, allow: r.allow || [], hostIps: r.host_ips || [] }
}
// Either may be undefined to leave it; a refused entry answers 400 with why.
export const putLan = (slug, { enabled, allow } = {}) => {
  const body = {}
  if (enabled !== undefined) body.enabled = !!enabled
  if (allow !== undefined) body.allow = allow
  return put(`/api/egress/lan/${enc(slug)}`, body)
}
