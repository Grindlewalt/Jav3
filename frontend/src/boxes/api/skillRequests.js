// Skill install requests (docs/agent-requests-contract.md section 2).
import { enc, get, post, qs } from '../http.js'

export async function listSkillRequests(status, project) {
  const r = await get(`/api/skill-requests${qs({ status, project })}`)
  return r.requests || []
}
// the host fetches and vets it now -> row (400 with the importer's reason)
export const fetchSkillRequest = (id) => post(`/api/skill-requests/${enc(id)}/fetch`)
// sends the hash of the text the operator read -> row
export const approveSkillRequest = (id, skillSha256) =>
  post(`/api/skill-requests/${enc(id)}/approve`, { acknowledge: true, skill_sha256: skillSha256 })
export const rejectSkillRequest = (id, reason) =>
  post(`/api/skill-requests/${enc(id)}/reject`, { reason: reason || '' })
