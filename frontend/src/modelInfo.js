// One cached read of the model catalogue for every surface that shows model
// names (the composer chips, the agent editor, the "answered by" tag, the
// providers card), so labels and prices come from the API — never hardcoded in
// JSX — and the page fetches it once. A switch (PUT) broadcasts
// `jarvis-model-changed` with the new state; the cache follows it, so every
// subscriber re-renders together.
//
// The catalogue is GET /api/models: `provider/model` ids with label, ctx and
// $/Mtok. A server that predates providers 404s there, and the cache is built
// from GET /api/model's {active, choices, options} instead — same shape out,
// the ids simply have no provider part. GET /api/model is read either way
// because it is the one that knows the runtime override (`active`);
// /api/models only promises the configured default.
//
// Normalised state:
//   { active, default, legacy,
//     models: [{id, label, provider, ctx, price_in, price_out, blurb}],
//     providers: {id: label} }
import { useEffect, useState } from 'react'
import { api } from './api.js'

let state = null
let pending = null
const labels = {}           // provider id -> display name (noteProviderLabels)
const subs = new Set()

function publish(next) {
  state = next
  subs.forEach((fn) => fn(next))
}

window.addEventListener('jarvis-model-changed', (e) => publish(e.detail))

const missing = (err) => { if (err?.status === 404) return null; throw err }
const tail = (id) => id.split('/').slice(1).join('/')

function fromLegacy(old) {
  const opts = old?.options || []
  const ids = old?.choices?.length ? old.choices : opts.map((o) => o.id)
  const models = ids.map((id) => {
    const o = opts.find((x) => x.id === id) || {}
    return { id, label: o.label || id, provider: '', ctx: null, price_in: null,
             price_out: null, blurb: o.blurb || '' }
  })
  return { active: old?.active || old?.default || '', default: old?.default || '',
           models, providers: {}, legacy: true }
}

function normalise(cat, old, providers) {
  if (!cat) return fromLegacy(old)
  const names = { ...providers }
  const models = (cat.models || []).filter((x) => x.enabled !== false).map((x) => {
    const provider = x.provider || (x.id.includes('/') ? x.id.split('/')[0] : '')
    const named = x.provider_label || x.provider_name
    if (named && !names[provider]) names[provider] = named
    return { id: x.id, label: x.label || tail(x.id) || x.id, provider,
             ctx: x.ctx ?? null, price_in: x.price_in ?? null,
             price_out: x.price_out ?? null, blurb: x.blurb || '' }
  })
  const m = { default: cat.default || '', models, providers: names, legacy: false }
  m.default = resolveId(m, m.default)
  m.active = resolveId(m, cat.active || old?.active || cat.default || '')
  return m
}

async function fetchState() {
  const [cat, old] = await Promise.all([
    api('/api/models').catch(missing),
    api('/api/model').catch(missing),
  ])
  if (!cat && !old) throw new Error('no model endpoint')
  return normalise(cat, old, { ...state?.providers, ...labels })
}

// Provider display names for the pickers' group headers. /api/models carries
// ids (and `provider_label` when the server sends it); GET /api/providers is
// the whole catalogue — thousands of models — so it is not fetched just for
// names: the providers card hands over the labels whenever it loads it, and
// until then a header shows the provider id.
export function noteProviderLabels(list) {
  let changed = false
  for (const p of list || []) {
    if (p.label && labels[p.id] !== p.label) { labels[p.id] = p.label; changed = true }
  }
  if (changed && state) publish({ ...state, providers: { ...state.providers, ...labels } })
}

export function loadModel() {
  if (state) return Promise.resolve(state)
  if (!pending) {
    pending = fetchState()
      .then((m) => { publish(m); return m })
      .finally(() => { pending = null })
  }
  return pending
}

// Re-read after the catalogue changed (a provider or model toggled, a new
// default) and tell every surface.
export async function reloadModels() {
  const next = await fetchState()
  window.dispatchEvent(new CustomEvent('jarvis-model-changed', { detail: next }))
  return next
}

// Switch the runtime model and tell every other surface. `id` is the
// `provider/model` id (a bare id on a pre-provider server).
export async function setModel(id) {
  const r = await api('/api/model', { method: 'PUT', body: JSON.stringify({ model: id }) })
  const base = state || fromLegacy(r)
  const next = r?.models
    ? normalise(r, r, base.providers)
    : { ...base, active: resolveId(base, r?.active || id) }
  window.dispatchEvent(new CustomEvent('jarvis-model-changed', { detail: next }))
  return next
}

// The catalogue id an id names. Bare ids (agent pins and messages written
// before providers) resolve to the default provider's model of that name,
// else the first provider carrying it; unknown ids come back unchanged.
export function resolveId(m, id) {
  if (!m || !id) return id
  const list = m.models || []
  if (id.includes('/') || list.some((x) => x.id === id)) return id
  const hits = list.filter((x) => tail(x.id) === id)
  if (!hits.length) return id
  const dp = (m.default || '').split('/')[0]
  return (hits.find((x) => x.provider === dp) || hits[0]).id
}

// Display copy for a model id: the catalogue entry, else the raw id.
export function modelOption(m, id) {
  const full = resolveId(m, id)
  return m?.models?.find((x) => x.id === full)
    || { id, label: id, provider: '', ctx: null, price_in: null, price_out: null, blurb: '' }
}

// Is this one of the catalogue's models (bare or qualified)?
export function isKnownModel(m, id) {
  const full = resolveId(m, id)
  return !!m && (full === m.default || m.models.some((x) => x.id === full))
}

export function providerLabel(m, pid) { return m?.providers?.[pid] || pid }

// The catalogue grouped by provider, in catalogue order:
// [{provider, label, models}].
export function modelGroups(m) {
  const out = []
  for (const x of m?.models || []) {
    let g = out.find((y) => y.provider === x.provider)
    if (!g) {
      g = { provider: x.provider, label: providerLabel(m, x.provider), models: [] }
      out.push(g)
    }
    g.models.push(x)
  }
  return out
}

// "128k" / "1M" — a context window in tokens, short.
export function ctxText(n) {
  if (!n) return ''
  if (n >= 1e6) return `${+(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M`
  return `${Math.round(n / 1e3)}k`
}

// two places ($1.10), three under a dime ($0.075)
const usd = (p) => `$${p > 0 && p < 0.1 ? +p.toFixed(3) : p.toFixed(2)}`

// "$0.27 / $1.10" per million tokens in / out; "free" for a zero-priced
// (local) model; '' when the catalogue has no price.
export function priceText(x) {
  if (x?.price_in == null && x?.price_out == null) return ''
  if (!x.price_in && !x.price_out) return 'free'
  return `${usd(x.price_in || 0)} / ${usd(x.price_out || 0)}`
}

// The dim caption under a model name in a picker: price, else the blurb.
export function modelCaption(x) {
  const p = priceText(x)
  if (p) return p === 'free' ? p : `${p} per Mtok`
  return x?.blurb || ''
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

// {default, active, models:[{id:"provider/model", label, provider, ctx,
// price_in, price_out}], providers} — the same cache under the plural name.
export const useModels = useModel
