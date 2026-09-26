// The package catalogue (contract J(f), WP5).
import { enc, get, post, qs } from '../http.js'

// -> [catalogue row + variant_used_by:[slugs]]
export async function listPackages(status) {
  const r = await get(`/api/packages${qs({ status })}`)
  return r.packages || []
}

// operator request: {manager, package, version?, reason, target_variant}
export const requestPackage = (body) => post('/api/packages', body)
export const approvePackage = (id, targetVariant) =>
  post(`/api/packages/${enc(id)}/approve`, { acknowledge: true, target_variant: targetVariant })
export const rejectPackage = (id, reason) =>
  post(`/api/packages/${enc(id)}/reject`, { reason: reason || '' })
