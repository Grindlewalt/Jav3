/* Settings → API providers: the provider catalogue (GET /api/providers,
 * backend/providers.py — models.dev's list, a couple of hundred providers and
 * thousands of models), laid out the way OpenCode does it: a search box over
 * everything, then Popular, then Configured (a key on file, or switched on),
 * then the rest folded behind "All providers (N)". A search opens every group
 * and filters them all.
 *
 * A row says what the provider is (label, kind), whether a key is on file,
 * how many of its models are on, and whether it is on; opening it gives the
 * key field (write-only — the stored key never comes back), the base URL
 * where one is needed, a Test, and the provider's models with their own
 * switch and the one default radio shared across every provider. Models are
 * off by default (only the global default is on), so a provider switched on
 * with none of its models on says "0 models on" in amber: the chips offer
 * nothing from it until some are.
 *
 * The first render reads `?models=0` (the full catalogue is ~1.5 MB); the
 * first row opened fetches the whole thing once for the session, and model
 * switches patch that copy in place rather than re-pulling it. Every change
 * re-reads the light list and the model cache (reloadModels), so the
 * composer chips and the agent editor follow without a reload. A server that
 * predates providers 404s here; the card then says so quietly and nothing
 * else on the page depends on it. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api.js'
import {
  ctxText, noteProviderLabels, priceText, reloadModels, useModels,
} from './modelInfo.js'
import { notifyError } from './notify.js'
import Button from './components/Button.jsx'
import Card from './components/Card.jsx'
import EmptyState from './components/EmptyState.jsx'
import Input from './components/Input.jsx'
import Tag from './components/Tag.jsx'
import Toggle from './components/Toggle.jsx'

// models.dev ids, in the order OpenCode shows them
const POPULAR = ['openai', 'anthropic', 'google', 'deepseek', 'openrouter', 'groq',
  'mistral', 'xai', 'togetherai', 'fireworks-ai', 'ollama', 'lmstudio']
const LOCAL_KINDS = ['ollama', 'lmstudio']
const MODEL_SEARCH_AT = 12
const isLocal = (p) => LOCAL_KINDS.includes(p.kind) || LOCAL_KINDS.includes(p.id)
// a base URL with a {VAR} in it (an account id, a resource name) is a
// template: the provider cannot be switched on until it is filled in
const templated = (url) => /\{[^}]+\}/.test(url || '')
const needsKey = (p) => (p.needs_key ?? !isLocal(p))
const needsUrl = (p) => templated(p.base_url) || (p.needs_base_url === true && !p.base_url)
const wantsUrlField = (p) => isLocal(p) || p.needs_base_url === true || templated(p.base_url)
const enc = encodeURIComponent
const norm = (t) => (t || '').toLowerCase()
const nameOf = (p) => p.label || p.id
const isDeprecated = (m) => m.deprecated === true || m.status === 'deprecated'

// set / not set / env. `key_set` is a boolean in the contract; a server that
// reports where the key came from may say 'env' there or in key_source.
function keyState(p) {
  if (p.key_set === 'env' || p.key_source === 'env') return 'env'
  return p.key_set ? 'set' : 'not set'
}
const configured = (p) => !!p.enabled || keyState(p) !== 'not set'

// "models.dev @ 2026-09-20" -> "models.dev, 2026-09-20"
const sourceText = (src) => (src || '').replace(/\s*@\s*/, ', ')

// provider id -> its models, from any payload that carries them
function modelMap(list) {
  const out = {}
  for (const p of list || []) if (Array.isArray(p.models)) out[p.id] = p.models
  return out
}

export default function ProvidersPanel() {
  const [data, setData] = useState(null)      // {providers, source} | {missing} | {error}
  const [models, setModels] = useState({})    // provider id -> models (the full fetch)
  const [full, setFull] = useState('no')      // 'no' | 'loading' | 'yes' | 'failed'
  const [open, setOpen] = useState(null)      // expanded provider id
  const [q, setQ] = useState('')
  const [all, setAll] = useState(false)       // "All providers" unfolded
  const catalogue = useModels()

  const load = useCallback(() => api('/api/providers?models=0')
    .then((r) => {
      setData({ providers: r.providers || [], source: r.catalog_source || '' })
      // a server that ignores ?models=0 has just sent the lot: keep it
      const got = modelMap(r.providers)
      if (Object.keys(got).length) { setModels(got); setFull('yes') }
      noteProviderLabels(r.providers)
    })
    .catch((e) => setData(e?.status === 404 ? { missing: true }
      : { error: e?.detail || e?.message || 'failed to load' })), [])
  useEffect(() => { load() }, [load])

  const loadFull = useCallback(() => {
    setFull('loading')
    return api('/api/providers')
      .then((r) => { setModels(modelMap(r.providers)); setFull('yes') })
      .catch(() => setFull('failed'))
  }, [])

  const toggleOpen = (id) => {
    setOpen((o) => (o === id ? null : id))
    if (full === 'no' || full === 'failed') loadFull()
  }

  const settle = () => { load(); reloadModels().catch(() => {}) }

  const putProvider = async (pid, body) => {
    try {
      await api(`/api/providers/${enc(pid)}`, { method: 'PUT', body: JSON.stringify(body) })
      return true
    } catch (e) { notifyError(e); return false }
    finally { settle() }
  }

  // a model switch patches the session's copy of the catalogue: a default is
  // one radio across every provider, so setting one clears the rest
  const putModel = async (pid, mid, body) => {
    try {
      await api(`/api/providers/${enc(pid)}/models/${enc(mid)}`,
        { method: 'PUT', body: JSON.stringify(body) })
      setModels((cur) => {
        const next = {}
        for (const [id, list] of Object.entries(cur)) {
          next[id] = list.map((m) => {
            const me = id === pid && m.id === mid
            let x = me ? { ...m, ...body } : m
            if (body.default && !me && m.default) x = { ...x, default: false }
            if (me && body.default) x = { ...x, enabled: true }
            return x
          })
        }
        return next
      })
    } catch (e) { notifyError(e) }
    finally { settle() }
  }

  const groups = useMemo(() => {
    const list = data?.providers || []
    const needle = norm(q.trim())
    const hit = (p) => !needle || norm(p.label).includes(needle) || norm(p.id).includes(needle)
    const byId = new Map(list.map((p) => [p.id, p]))
    const popular = POPULAR.map((id) => byId.get(id)).filter(Boolean)
    const taken = new Set(popular.map((p) => p.id))
    const mine = list.filter((p) => !taken.has(p.id) && configured(p))
    mine.forEach((p) => taken.add(p.id))
    const rest = list.filter((p) => !taken.has(p.id))
      .sort((x, y) => norm(nameOf(x)).localeCompare(norm(nameOf(y))))
    return { popular: popular.filter(hit), mine: mine.filter(hit),
             rest: rest.filter(hit), restTotal: rest.length }
  }, [data, q])

  const row = (p) => (
    <ProviderRow key={p.id} p={p} models={models[p.id]} catalogue={catalogue} full={full}
                 open={open === p.id}
                 onOpen={() => toggleOpen(p.id)} onRetry={loadFull}
                 putProvider={(b) => putProvider(p.id, b)}
                 putModel={(mid, b) => putModel(p.id, mid, b)}
                 onTested={() => { if (full === 'yes') loadFull() }} />
  )
  const searching = q.trim() !== ''
  const none = !groups.popular.length && !groups.mine.length && !groups.rest.length

  let body
  if (data === null) body = <EmptyState>loading…</EmptyState>
  else if (data.missing) body = <EmptyState>Provider settings are not available on this server yet.</EmptyState>
  else if (data.error) body = <EmptyState>Couldn’t load providers: {data.error}</EmptyState>
  else {
    body = (
      <div className="stack">
        <p className="dim small settings-note">
          Keys are stored on this server and never enter the sandbox VM.
          {data.source && <> Prices: {sourceText(data.source)}.</>}</p>
        <Input type="search" aria-label="Search providers" placeholder="Search providers"
               value={q} onChange={(e) => setQ(e.target.value)} />
        {none && <EmptyState>No provider matches “{q.trim()}”.</EmptyState>}
        {groups.popular.length > 0 && (
          <section className="prov-group" aria-label="Popular">
            <h3 className="prov-group-head">Popular</h3>
            <ul className="prov-list">{groups.popular.map(row)}</ul>
          </section>
        )}
        {groups.mine.length > 0 && (
          <section className="prov-group" aria-label="Configured">
            <h3 className="prov-group-head">Configured</h3>
            <ul className="prov-list">{groups.mine.map(row)}</ul>
          </section>
        )}
        {groups.rest.length > 0 && (
          <section className="prov-group" aria-label="All providers">
            {searching ? <h3 className="prov-group-head">All providers</h3> : (
              <button type="button" className="prov-all" aria-expanded={all}
                      onClick={() => setAll((v) => !v)}>
                All providers ({groups.restTotal})
                <span className={all ? 'chev open' : 'chev'} aria-hidden="true">›</span>
              </button>
            )}
            {(searching || all) && <ul className="prov-list">{groups.rest.map(row)}</ul>}
          </section>
        )}
      </div>
    )
  }

  return (
    <Card title="API providers" headingLevel={2} id="providers" className="prov-card">
      {body}
    </Card>
  )
}

// How many of a provider's models are on: from its models once they are
// loaded, else from the model cache (GET /api/models lists only enabled
// models), else unknown.
function enabledCount(p, models, catalogue) {
  if (models) return models.filter((m) => m.enabled).length
  if (catalogue && !catalogue.legacy) {
    return catalogue.models.filter((m) => m.provider === p.id).length
  }
  return null
}

function ProviderRow({
  p, models, catalogue, full, open, onOpen, onRetry, putProvider, putModel, onTested,
}) {
  const key = keyState(p)
  const blocked = needsUrl(p)
  const on = enabledCount(p, models, catalogue)
  return (
    <li className={open ? 'prov-row open' : 'prov-row'}>
      <div className="prov-head">
        <button type="button" className="prov-toggle" aria-expanded={open}
                aria-controls={`prov-${p.id}`} onClick={onOpen}>
          <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
          <strong className="ellipsis">{nameOf(p)}</strong>
        </button>
        <div className="prov-facts">
          {p.kind && <Tag>{p.kind}</Tag>}
          {needsKey(p) && (
            <span className={`small prov-key ${key === 'not set' ? '' : 'ok'}`}
                  title={key === 'env' ? 'key comes from the server environment' : undefined}>
              key {key}</span>
          )}
          {blocked && <span className="small prov-key warn">needs base URL</span>}
          {p.enabled && on !== null && (
            <span className={on ? 'dim small' : 'small prov-key warn'}>
              {on} model{on === 1 ? '' : 's'} on</span>
          )}
          {p.docs && (
            <a className="prov-docs" href={p.docs} target="_blank" rel="noreferrer noopener"
               aria-label={`${nameOf(p)} docs`} title="docs">↗</a>
          )}
        </div>
        <Toggle label={`${nameOf(p)} enabled`} checked={!!p.enabled}
                disabled={blocked && !p.enabled}
                title={blocked ? 'fill in the base URL first' : undefined}
                onChange={(v) => putProvider({ enabled: v })} />
      </div>
      {open && (
        <div className="prov-body" id={`prov-${p.id}`}>
          {needsKey(p) && <KeyField p={p} keyState={key} putProvider={putProvider} />}
          {wantsUrlField(p) && (
            <BaseUrlField p={p} required={p.needs_base_url === true || templated(p.base_url)}
                          putProvider={putProvider} />
          )}
          <TestLine p={p} onTested={onTested} />
          {models ? (models.length > 0 && <ModelList p={p} models={models} putModel={putModel} />)
            : full === 'failed' ? (
              <div className="settings-actions">
                <span className="small prov-result bad">Couldn’t load the models.</span>
                <Button variant="ghost" onClick={onRetry}>Retry</Button>
              </div>
            ) : full === 'loading' ? <span className="dim small">loading models…</span> : null}
        </div>
      )}
    </li>
  )
}

// Paste, Save, gone: the field is write-only and clears once the server has
// the key, so a key never sits in the page longer than it takes to save it.
function KeyField({ p, keyState: key, putProvider }) {
  const [val, setVal] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async (e) => {
    e.preventDefault()
    if (!val.trim()) return
    setBusy(true)
    if (await putProvider({ api_key: val.trim() })) setVal('')
    setBusy(false)
  }
  return (
    <form className="settings-inline" onSubmit={save}>
      <Input type="password" autoComplete="off" spellCheck={false}
             aria-label={`${nameOf(p)} API key`} value={val}
             placeholder={key === 'not set' ? 'paste API key' : 'paste a new key to replace'}
             onChange={(e) => setVal(e.target.value)} />
      <div className="settings-inline-actions">
        <Button type="submit" disabled={busy || !val.trim()}>Save</Button>
      </div>
    </form>
  )
}

// Local kinds get an optional override (the default is the placeholder); a
// provider whose URL is a template, or that has none, must have one — the
// template itself is the placeholder, and a value still holding {VAR} is
// refused before it is sent.
function BaseUrlField({ p, required, putProvider }) {
  const fallback = p.default_base_url || p.base_url || ''
  const initial = templated(p.base_url) || !p.base_url || p.base_url === p.default_base_url
    ? '' : p.base_url
  const [val, setVal] = useState(initial)
  useEffect(() => { setVal(initial) }, [initial])
  const bad = templated(val) || (required && !val.trim() && !p.base_url)
  const save = (e) => {
    e.preventDefault()
    if (!bad) putProvider({ base_url: val.trim() || null })
  }
  return (
    <form className="settings-inline" onSubmit={save}>
      <Input type="url" spellCheck={false} required={required && !initial}
             aria-label={`${nameOf(p)} base URL${required ? ' (required)' : ''}`}
             value={val} placeholder={fallback || 'base URL'}
             aria-invalid={templated(val) ? true : undefined}
             onChange={(e) => setVal(e.target.value)} />
      <div className="settings-inline-actions">
        <Button type="submit" disabled={val === initial || bad}>Save</Button>
      </div>
    </form>
  )
}

function TestLine({ p, onTested }) {
  const [res, setRes] = useState(null)       // {ok, text} | 'busy'
  const test = async () => {
    setRes('busy')
    try {
      const r = await api(`/api/providers/${enc(p.id)}/test`, { method: 'POST' })
      const n = r.models_found?.length ?? 0
      setRes(r.ok ? { ok: true, text: `ok · ${n} model${n === 1 ? '' : 's'}` }
        : { ok: false, text: r.detail || 'failed' })
      if (r.ok && n) onTested()          // found models join the list
    } catch (e) {
      setRes({ ok: false, text: e?.status === 404 ? 'test not available yet'
        : (e?.detail || e?.message || 'failed') })
    }
  }
  return (
    <div className="settings-actions prov-test">
      <Button variant="ghost" disabled={res === 'busy'} onClick={test}>
        {res === 'busy' ? 'Testing…' : 'Test'}</Button>
      {res && res !== 'busy' && (
        <span className={`small prov-result ${res.ok ? 'ok' : 'bad'}`} role="status">
          {res.text}</span>
      )}
    </div>
  )
}

// A provider's models (bare ids here; the chips use provider/model). Past a
// dozen there is a search; deprecated models are hidden unless asked for — an
// enabled or default one always shows, so a switch that is on can always be
// switched off. The default's own switch is locked on: turn another model
// into the default first.
function ModelList({ p, models: all, putModel }) {
  const [q, setQ] = useState('')
  const [old, setOld] = useState(false)
  const deprecated = all.filter(isDeprecated).length
  const needle = norm(q.trim())
  const shown = all.filter((m) => (old || !isDeprecated(m) || m.enabled || m.default)
    && (!needle || norm(m.label).includes(needle) || norm(m.id).includes(needle)))
  return (
    <div className="prov-models-wrap">
      {(all.length > MODEL_SEARCH_AT || deprecated > 0) && (
        <div className="prov-models-tools">
          {all.length > MODEL_SEARCH_AT && (
            <Input type="search" aria-label={`Search ${nameOf(p)} models`}
                   placeholder={`Search ${all.length} models`} value={q}
                   onChange={(e) => setQ(e.target.value)} />
          )}
          {deprecated > 0 && (
            <Toggle label="show deprecated models" checked={old} onChange={setOld}
                    onText={`Deprecated (${deprecated})`} offText={`Deprecated (${deprecated})`} />
          )}
        </div>
      )}
      <ul className="prov-models" aria-label={`${nameOf(p)} models`}>
        {shown.length === 0 && <EmptyState as="li">No model matches.</EmptyState>}
        {shown.map((m) => {
          const price = priceText(m)
          const canDefault = p.enabled && m.enabled
          const meta = [ctxText(m.ctx) && `${ctxText(m.ctx)} ctx`,
            price && (price === 'free' ? price : `${price} per Mtok`),
            m.discovered && 'found by Test'].filter(Boolean)
          return (
            <li key={m.id} className="prov-model">
              <div className="prov-model-main">
                <span className="ellipsis" title={m.id}>{m.label || m.id}</span>
                {meta.length > 0 && <span className="dim small">{meta.join(' · ')}</span>}
              </div>
              <label className={canDefault ? 'prov-default' : 'prov-default off'}
                     title={canDefault ? 'the model new turns use'
                       : p.enabled ? 'switch the model on first' : 'switch the provider on first'}>
                <input type="radio" name="provider-default-model" checked={!!m.default}
                       disabled={!canDefault}
                       onChange={() => putModel(m.id, { default: true })} />
                <span>default</span>
              </label>
              <Toggle label={`${m.label || m.id} enabled`} checked={!!m.enabled}
                      disabled={!!m.default}
                      onChange={(v) => putModel(m.id, { enabled: v })} />
            </li>
          )
        })}
      </ul>
    </div>
  )
}
