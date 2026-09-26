// Image variants and versions (contract J(e) Images, WP5).
import { enc, follow, get, post } from '../http.js'

// -> {variants:[{name, from, builtin, recipe, recipe_sha256, min_mem_mb,
//     used_by:[slugs], versions:[{version, base_version, size_bytes, built_at,
//     status, active, in_use_by:[box ids]}]}], build:{running, variant, phase}}
export async function listImages() {
  const r = await get('/api/vm/images')
  return { variants: r.variants || [], build: r.build || { running: false } }
}

// a new variant: {name, from, packages:[{manager, package, version}]}
export const createVariant = (body) => post('/api/vm/images', body)
export const buildVariant = (variant) =>
  post(`/api/vm/images/${enc(variant)}/build`, { confirm: true })

// Build progress: bus `vm-images`, assumed to ride /api/events as topic
// "vm-images" (not yet in the contract). The page also polls while a build
// runs, so a missing topic only costs latency.
export const followBuilds = (fn) => follow('vm-images', fn)
