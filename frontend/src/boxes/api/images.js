// Image variants and versions (docs/boxes-api-final.md section 5, WP5).
import { enc, follow, get, post } from '../http.js'

// GET /api/vm/images ->
//   {variants:[{name, from, builtin, recipe, recipe_sha256, min_mem_mb,
//     layer_packages, needs_build, used_by:[slugs], versions:[{version,
//     base_version, size_bytes, built_at, status, active, recipe_sha256,
//     in_use_by:[box ids]}]}],
//    build:{running, variant, mode, phase, log_tail:[str]}}
export async function listImages() {
  const r = await get('/api/vm/images')
  const b = r.build || {}
  return {
    variants: r.variants || [],
    build: {
      running: !!b.running, variant: b.variant || null, mode: b.mode || null,
      phase: b.phase || null, log_tail: Array.isArray(b.log_tail) ? b.log_tail : [],
    },
  }
}

// a new variant: {name, from, packages:[{manager, package, version}]}
// -> {name, from, recipe_sha256, ...}
export const createVariant = (body) => post('/api/vm/images', body)
// -> {started: true, variant}
export const buildVariant = (variant) =>
  post(`/api/vm/images/${enc(variant)}/build`, { confirm: true })
// -> {variant, recipe_sha256, dockerfile} (the docker runtime's recipe)
export const variantDockerfile = (variant) => get(`/api/vm/images/${enc(variant)}/dockerfile`)
// One build's whole log: the running build, else that version's (or the newest
// finished one's). Variant `base` = the golden image's rebuild since the app
// started. -> {variant, version, running, phase, ok, error, finished_at,
// lines:[str], source}. The lines are builder output: render as text only.
export const imageLog = (variant, version = null) =>
  get(`/api/vm/images/${enc(variant)}/log${version != null ? `?version=${enc(version)}` : ''}`)

// Live: topic `vm-images` on the shared stream: {type:"image_build", phase,
// variant, box?, line?, ...}. Fold with logic.applyBuildEvent.
export const followBuilds = (fn) => follow('vm-images', (ev) => {
  if (ev && ev.type === 'image_build') fn(ev)
})
