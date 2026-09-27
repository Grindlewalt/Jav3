// Service boxes (docs/boxes-api-final.md section 4, WP3).
import { call, enc, follow, get, post, qs } from '../http.js'

// Live: topic `services` on the ONE shared /api/events stream.
// {type: "service_changed", service_id, project, status, desired_state} or
// {type: "service_state", service_id, state, error}, plus the feed's
// stream_open on every (re)connect. Each is a cue to refetch listServices.
export const followServices = (fn) => follow('services', fn)

// The fallback poll beside the topic (a dropped stream, a missed event).
export const SERVICES_POLL_MS = 60000

// a POST with no body at all (start/stop take none)
const call0 = (path) => call(path, { method: 'POST' })

// GET /api/services?project= ->
//   {services:[row], services_lan_ip: "" | ip, services_lan_ip_configured,
//    lan_error: str|null, relays:[{service_id, port, bind, address, box_id,
//    listening, conns, bytes_in, bytes_out, error}]}
// `services_lan_ip` is non-empty only when the configured address passed the
// host's checks; `lan_error` says why a configured one did not.
export async function listServices(project) {
  const r = await get(`/api/services${qs({ project })}`)
  return {
    services: r.services || [],
    lanIp: r.services_lan_ip || '',
    lanConfigured: r.services_lan_ip_configured || '',
    lanError: r.lan_error || null,
    relays: r.relays || [],
  }
}
// -> row + {diff: str|null, relays}
export const getService = (id) => get(`/api/services/${enc(id)}`)
// placement is REQUIRED; expose_ports is REQUIRED and may be []. Each entry
// is {port, bind: "loopback"|"lan"} — the canonical values (logic.exposePayload).
export const approveService = (id, { placement, exposePorts }) =>
  post(`/api/services/${enc(id)}/approve`,
    { acknowledge: true, placement, expose_ports: exposePorts || [] })
export const rejectService = (id, reason) =>
  post(`/api/services/${enc(id)}/reject`, { reason: reason || '' })
// no body
export const startService = (id) => call0(`/api/services/${enc(id)}/start`)
export const stopService = (id) => call0(`/api/services/${enc(id)}/stop`)
// -> row + {data_deleted}
export const revokeService = (id, deleteData) =>
  post(`/api/services/${enc(id)}/revoke`, { confirm: true, delete_data: !!deleteData })
// -> {service_id, untrusted: true, text}: the guest's journal. Text only.
export async function serviceLogs(id, lines = 200) {
  const r = await get(`/api/services/${enc(id)}/logs${qs({ lines })}`)
  return { text: String(r.text ?? ''), untrusted: r.untrusted !== false }
}
