// Service boxes (contract J(a), WP3).
import { enc, get, post, qs } from '../http.js'

// -> {services:[...], lanIp}. `lanIp` is settings.services_lan_ip, which is
// NOT in the contract's HTTP shapes yet: it is read from the list response's
// top-level `services_lan_ip` (or `lan_ip`). Absent = LAN exposure
// unavailable, which is also the safe reading.
export async function listServices(project) {
  const r = await get(`/api/services${qs({ project })}`)
  return { services: r.services || [], lanIp: r.services_lan_ip || r.lan_ip || '' }
}
// -> row + {diff}
export const getService = (id) => get(`/api/services/${enc(id)}`)
// expose: [{port, bind:"loopback"|"lan"}] — ports left at "none" are omitted
export const approveService = (id, { placement, expose }) =>
  post(`/api/services/${enc(id)}/approve`, { acknowledge: true, placement, expose_ports: expose })
export const rejectService = (id, reason) =>
  post(`/api/services/${enc(id)}/reject`, { reason: reason || '' })
export const startService = (id) => post(`/api/services/${enc(id)}/start`)
export const stopService = (id) => post(`/api/services/${enc(id)}/stop`)
export const revokeService = (id, deleteData) =>
  post(`/api/services/${enc(id)}/revoke`, { confirm: true, delete_data: !!deleteData })
