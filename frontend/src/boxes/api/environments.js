// Environment requests (docs/agent-requests-contract.md section 1).
import { enc, get, post, qs } from '../http.js'

// -> [row]; a row carries packages:[...], resolved, plan, profile, build
export async function listEnvironments(status, project) {
  const r = await get(`/api/environments${qs({ status, project })}`)
  return r.environments || []
}
// -> row + {build_started}
export const approveEnvironment = (id, { build = true } = {}) =>
  post(`/api/environments/${enc(id)}/approve`, { acknowledge: true, build })
export const rejectEnvironment = (id, reason) =>
  post(`/api/environments/${enc(id)}/reject`, { reason: reason || '' })
