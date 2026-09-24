// One cached read of GET /api/model for every surface that shows model names
// (Settings, the composer chip, the agent editor, the "answered by" tag), so
// labels come from config via the API — never hardcoded in JSX — and the page
// fetches it once. A switch (PUT) broadcasts `jarvis-model-changed` with the
// new state; the cache follows it, so every subscriber re-renders together.
import { useEffect, useState } from 'react'
import { api } from './api.js'

let state = null
let pending = null
const subs = new Set()

function publish(next) {
  state = next
  subs.forEach((fn) => fn(next))
}

window.addEventListener('jarvis-model-changed', (e) => publish(e.detail))

export function loadModel() {
  if (state) return Promise.resolve(state)
  if (!pending) {
    pending = api('/api/model')
      .then((m) => { publish(m); return m })
      .finally(() => { pending = null })
  }
  return pending
}

// Switch the runtime model and tell every other surface.
export async function setModel(model) {
  const next = await api('/api/model', { method: 'PUT', body: JSON.stringify({ model }) })
  window.dispatchEvent(new CustomEvent('jarvis-model-changed', { detail: next }))
  return next
}

// Display copy for a model id: the configured label/blurb, else the raw id.
export function modelOption(m, id) {
  return m?.options?.find((o) => o.id === id) || { id, label: id, blurb: '' }
}

export function useModel() {
  const [m, setM] = useState(state)
  useEffect(() => {
    subs.add(setM)
    loadModel().then(setM).catch(() => {})
    return () => { subs.delete(setM) }
  }, [])
  return m
}
