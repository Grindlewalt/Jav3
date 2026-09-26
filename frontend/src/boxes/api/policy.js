// Per-project egress allow/deny (contract J(c), WP2).
import { enc, get, post, put } from '../http.js'
import { updateProfile } from './profiles.js'

// -> {profile:{id,name,default}, project_allow, project_deny, effective_allow,
//     effective_deny}
export async function getPolicy(slug) {
  const p = await get(`/api/egress/policy/${enc(slug)}`)
  return {
    profile: p.profile || null,
    project_allow: p.project_allow || [],
    project_deny: p.project_deny || [],
    effective_allow: p.effective_allow || [],
    effective_deny: p.effective_deny || [],
  }
}
export const putPolicy = (slug, allow, deny) =>
  put(`/api/egress/policy/${enc(slug)}`, { allow, deny })

// GET /api/egress/allowlist: "groups by project, then profile". Assumed:
// {groups:[{project, profile?:{id,name}, entries:[{host, source, ...}],
//           deny?:[host|{host}]}]} — the same groups/entries the page read
// before, plus a per-project `deny`. logic.groupPolicy tolerates both.
export async function allowlist() {
  const r = await get('/api/egress/allowlist')
  return r.groups || []
}
export const revokeAllow = (body) => post('/api/egress/allowlist/revoke', body)
export const promoteAuto = (id) => post(`/api/egress/auto/${enc(id)}/promote`)

// Promote a host from a project's list to its profile's baseline: add it to
// the profile (PUT /api/profiles/{id} with the whole row), then take it off
// the project's list. Two calls: the contract has no dedicated route.
export async function promoteToProfile(profile, slug, host, kind = 'allow') {
  const field = kind === 'deny' ? 'deny_hosts' : 'allow_hosts'
  const { id, builtin, projects, ...row } = profile   // eslint-disable-line no-unused-vars
  await updateProfile(id, { ...row, [field]: [...new Set([...(row[field] || []), host])] })
  const pol = await getPolicy(slug)
  await putPolicy(slug,
    pol.project_allow.filter((h) => !(kind === 'allow' && h === host)),
    pol.project_deny.filter((h) => !(kind === 'deny' && h === host)))
}
