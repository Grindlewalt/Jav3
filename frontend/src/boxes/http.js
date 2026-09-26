// The one door every boxes feature goes through to the server.
//
// Each feature has its own module under ./api/ (vms, images, packages,
// services, procs, profiles, policy, persist) and each of those speaks only
// through this file, so reconciling a feature with the backend is a change to
// that one module.
//
// Mock mode: while WP1–WP5/WP8 land in parallel, `?mock=1` on any URL (sticky
// in localStorage; `?mock=0` clears it) answers every boxes call from
// ./mock.js — the shapes in docs/boxes-contract.md — instead of the server.
// Nothing else in the app is mocked, and the mock chunk is only loaded when
// it is on.
import { api, ApiError } from '../api.js'
import { subscribe } from '../events.js'

const KEY = 'jav3.mock'

export function mockOn() {
  try {
    const q = new URLSearchParams(window.location.search).get('mock')
    if (q === '1') localStorage.setItem(KEY, '1')
    else if (q === '0') localStorage.removeItem(KEY)
    return localStorage.getItem(KEY) === '1'
  } catch { return false }
}

let mockMod = null
async function mock() {
  mockMod = mockMod || await import('./mock.js')
  return mockMod
}

export async function call(path, options = {}) {
  if (mockOn()) return (await mock()).handle(path, options)
  return api(path, options)
}

const json = (method) => (path, body) =>
  call(path, { method, body: JSON.stringify(body ?? {}) })
export const get = (path) => call(path)
export const post = json('POST')
export const put = json('PUT')
export const del = (path) => call(path, { method: 'DELETE' })

// "the backend has not shipped this route yet" — pages show a quiet notice
// instead of an error toast
export const missing = (e) => e instanceof ApiError && (e.status === 404 || e.status === 405)

// A topic on the ONE shared event stream (events.js). Never a new
// EventSource. In mock mode, the mock feeds the same callback.
export function follow(topic, fn) {
  if (mockOn()) {
    let stop = () => {}
    let dead = false
    mock().then((m) => { if (!dead) stop = m.follow(topic, fn) })
    return () => { dead = true; stop() }
  }
  return subscribe(topic, fn)
}

export const qs = (params) => {
  const s = new URLSearchParams(Object.entries(params || {})
    .filter(([, v]) => v != null && v !== '')).toString()
  return s ? `?${s}` : ''
}
export const enc = encodeURIComponent
