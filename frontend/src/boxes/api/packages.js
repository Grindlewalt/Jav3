// The package catalogue (docs/boxes-api-final.md section 5, WP5).
import { enc, get, post, qs } from '../http.js'

// -> [row]; a row carries variant_used_by:[slugs], variant_used_by_detail:
// {all, direct, via:{variant:[slugs]}} and `card`, the server's one-line
// reach sentence ("installs into `dev` — used by: a, b").
export async function listPackages(status) {
  const r = await get(`/api/packages${qs({ status })}`)
  return r.packages || []
}

// The operator adds packages: {packages:[{manager, package, version}], reason,
// target_variant?} or, for a new variant, {..., new_variant, from}.
// -> {packages:[rows], skipped:[{package, error}], target_variant}
export async function requestPackages(body) {
  const r = await post('/api/packages', body)
  return {
    packages: r.packages || [], skipped: r.skipped || [],
    target_variant: r.target_variant || body.target_variant || body.new_variant || null,
  }
}
// dry-run every unresolved pending row in a builder box -> {resolved, error?}
export const resolvePackages = () => post('/api/packages/resolve')
// -> row + {variant_used_by, build_started}
export const approvePackage = (id, { targetVariant, build = false } = {}) =>
  post(`/api/packages/${enc(id)}/approve`, {
    acknowledge: true,
    ...(targetVariant ? { target_variant: targetVariant } : {}),
    ...(build ? { build: true } : {}),
  })
export const rejectPackage = (id, reason) =>
  post(`/api/packages/${enc(id)}/reject`, { reason: reason || '' })
// approved | built | failed -> removed (the variant's next build drops it)
export const removePackage = (id) => post(`/api/packages/${enc(id)}/remove`)
