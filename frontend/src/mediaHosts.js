// Single source of truth for which remote media hosts may auto-load. Populated
// once at app boot from /api/config, then read by both render surfaces — chat
// markdown (Md.jsx) and the dashboard iframe CSP (Workspace.jsx) — so the policy
// lives in one place. A same-origin path or an inert data: image is always fine
// (no third-party fetch); any other host must be on the allowlist or it is
// blocked, which is what stops a model from beaconing data out via a resource URL.
let allowed = []

export function setMediaHosts(list) {
  allowed = Array.isArray(list) ? list.map((h) => String(h).toLowerCase()) : []
}

const LOCAL_RESOURCE = /^(?:\/(?!\/)|\.\.?\/|data:image\/)/i

function hostOf(value) {
  try {
    return new URL(value, window.location.origin).hostname.toLowerCase()
  } catch {
    return ''
  }
}

// May this resource URL load automatically in chat?
export function resourceAllowed(value) {
  const v = (value || '').trim()
  if (!v) return false
  if (LOCAL_RESOURCE.test(v)) return true
  const host = hostOf(v)
  return !!host && allowed.some((h) => host === h || host.endsWith('.' + h))
}

// CSP source list for img-src/media-src/font-src inside the Renderer iframe.
// Bare host with ":*" matches the host over http or https on any port, which a
// homelab (e.g. the NAS on a nonstandard port) needs.
export function cspMediaSources() {
  return ['data:', ...allowed.map((h) => `${h}:*`)].join(' ')
}
