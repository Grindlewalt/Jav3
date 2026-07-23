import { useEffect, useRef, useState } from 'react'
import { api, subscribeSse } from '../api.js'

// The guest's live egress: a scrolling feed of every outbound request the
// sandbox made, with a verdict chip (allow / deny / cut), an approval queue for
// hosts the guest keeps trying to reach ("training the allowlist up"), and a
// per-project policy + secret-grant editor.
//
// Every string here — host, path, project, reason — is UNTRUSTED (it is guest
// traffic). It is only ever rendered as plain text nodes, never markup.

const FEED_CAP = 300

function human(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / (1024 * 1024)).toFixed(1)} MB`
}

// allow -> green, cut -> red, everything else (deny / anomaly) -> amber
const verdictClass = (v) => (v === 'allow' ? 'allow' : v === 'cut' ? 'cut' : 'deny')

function FeedRow({ e }) {
  const cls = verdictClass(e.verdict)
  return (
    <div className={`egr-row ${cls === 'allow' ? '' : cls}`}>
      <span className={`egr-chip ${cls}`}>{e.verdict}</span>
      <span className="egr-host" title={e.host}>{e.host}</span>
      {e.project && <span className="tag">{e.project}</span>}
      <span className="egr-path" title={`${e.method || ''} ${e.path || ''}`}>
        {e.method ? `${e.method} ` : ''}{e.path}</span>
      {e.reason && <span className="egr-reason" title={e.reason}>{e.reason}</span>}
      <span className="egr-bytes" title="out / in">
        ↑{human(e.bytes_out)} ↓{human(e.bytes_in)}</span>
    </div>
  )
}

// ---- per-project egress policy editor ---------------------------------------
function PolicyEditor({ slug }) {
  const [pol, setPol] = useState(null)
  const [hostsText, setHostsText] = useState('')
  const [status, setStatus] = useState('')
  const [saving, setSaving] = useState(false)

  function load() {
    api(`/api/egress/policy/${slug}`).then((p) => {
      setPol(p); setHostsText((p.hosts || []).join('\n'))
    }).catch(() => setPol(null))
  }
  useEffect(() => { load() }, [slug]) // eslint-disable-line

  async function save() {
    setSaving(true)
    const hosts = hostsText.split(/[\s,]+/).map((h) => h.trim()).filter(Boolean)
    try {
      await api(`/api/egress/policy/${slug}`, {
        method: 'PUT',
        body: JSON.stringify({ mode: pol.mode, inherit_general: pol.inherit_general, hosts }) })
      setStatus('saved'); setTimeout(() => setStatus(''), 1500); load()
    } catch (err) { window.alert(err.detail || String(err)) }
    setSaving(false)
  }

  if (!pol) return null
  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Policy · {slug}</h3><span className="dim small">{status}</span></div>
      <div className="net-policy">
        <label className="mini">mode
          <select value={pol.mode || 'allowlist'}
                  onChange={(e) => setPol({ ...pol, mode: e.target.value })}>
            <option value="allowlist">allowlist — only listed hosts</option>
            <option value="denylist">denylist — all but listed hosts</option>
            <option value="denyall">deny all — no egress</option>
          </select>
        </label>
        <label className="incognito-toggle">
          <input type="checkbox" checked={!!pol.inherit_general}
                 onChange={(e) => setPol({ ...pol, inherit_general: e.target.checked })} />
          inherit the general allowlist
        </label>
        <label className="mini">hosts (one per line)
          <textarea className="md-editor" rows={4} spellCheck={false} value={hostsText}
                    onChange={(e) => setHostsText(e.target.value)} />
        </label>
        <div className="row">
          <span className="dim small grow">
            effective: {pol.mode}{pol.source ? ` · source: ${pol.source}` : ''}</span>
          <button disabled={saving} onClick={save}>{saving ? '…' : 'Save policy'}</button>
        </div>
        {Array.isArray(pol.effective) && pol.effective.length > 0 && (
          <div className="dim small">effective hosts: {pol.effective.join(', ')}</div>
        )}
      </div>
    </div>
  )
}

// ---- per-project secret grants ----------------------------------------------
function Grants({ slug }) {
  const [grants, setGrants] = useState([])
  function load() {
    api(`/api/egress/grants/${slug}`).then((r) => setGrants(r.grants || [])).catch(() => setGrants([]))
  }
  useEffect(() => { load() }, [slug]) // eslint-disable-line

  async function set(secret, statusVal) {
    try {
      await api(`/api/egress/grants/${slug}`, {
        method: 'POST', body: JSON.stringify({ secret, status: statusVal }) })
      load()
    } catch (err) { window.alert(err.detail || String(err)) }
  }
  async function add() {
    const name = window.prompt('secret name to grant to this project')
    if (!name) return
    set(name.trim(), 'granted')
  }

  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Secret grants · {slug}</h3>
        <button className="ghost" onClick={add}>+ grant</button></div>
      <ul className="staged-list rev-list">
        {grants.length === 0 && <li className="dim" style={{ cursor: 'default' }}>none granted</li>}
        {grants.map((g) => {
          const granted = g.status === 'granted'
          return (
            <li key={g.secret_name}>
              <span className={`tag ${granted ? 'done' : ''}`}>{g.status}</span>
              <span className="grow ellipsis mono">{g.secret_name}</span>
              <button className="ghost"
                      onClick={() => set(g.secret_name, granted ? 'revoked' : 'granted')}>
                {granted ? 'revoke' : 'grant'}</button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// Compact, project-scoped egress view for a Workspace panel: the live feed
// filtered to this project, its host-approval queue, policy + grants. Same data
// and endpoints as the full Network page, no project picker.
export function NetworkPanel({ slug }) {
  const [feed, setFeed] = useState([])
  const [pending, setPending] = useState([])
  const keyRef = useRef(0)

  useEffect(() => {
    let live = true
    api('/api/egress/events?limit=200').then((r) => {
      const evs = (Array.isArray(r) ? r : r.events) || []
      // REST rows carry project_slug; the live stream carries project
      if (live) setFeed(evs.map((e) => ({ ...e, project: e.project ?? e.project_slug }))
        .filter((e) => e.project === slug)
        .map((e) => ({ ...e, _k: ++keyRef.current })))
    }).catch(() => {})
    const stop = subscribeSse('/api/egress/stream', (ev) => {
      if (ev.type !== 'egress' || ev.project !== slug) return
      setFeed((f) => [{ ...ev, _k: ++keyRef.current }, ...f].slice(0, FEED_CAP))
    })
    return () => { live = false; stop() }
  }, [slug])

  const reloadPending = () =>
    api(`/api/egress/pending?project=${encodeURIComponent(slug)}`)
      .then((r) => setPending(r.pending || [])).catch(() => {})
  useEffect(() => {
    reloadPending()
    const t = setInterval(reloadPending, 10000)
    return () => clearInterval(t)
  }, [slug]) // eslint-disable-line
  async function decide(id, verb) {
    try { await api(`/api/egress/pending/${id}/${verb}`, { method: 'POST' }); reloadPending() }
    catch (err) { window.alert(err.detail || String(err)) }
  }

  return (
    <div className="pane-col net-panel">
      <div className="sbx-card">
        <div className="sbx-sec-head"><h3>Host approvals</h3>
          <span className="dim small">{pending.length} waiting</span></div>
        <div className="dim small">hosts the agent's code tried to reach — approve to
          let it through (trains the allowlist), reject to keep it out</div>
        <ul className="staged-list rev-list">
          {pending.length === 0 && <li className="dim" style={{ cursor: 'default' }}>nothing waiting</li>}
          {pending.map((p) => (
            <li key={p.id}>
              <span className="tag pending">{p.hit_count}×</span>
              <span className="grow ellipsis mono" title={p.host}>{p.host}</span>
              <button className="win-btn ok" title="approve" onClick={() => decide(p.id, 'approve')}>✓</button>
              <button className="win-btn" title="reject" onClick={() => decide(p.id, 'reject')}>✕</button>
            </li>
          ))}
        </ul>
      </div>
      <PolicyEditor slug={slug} />
      <Grants slug={slug} />
      <div className="dim small" style={{ marginTop: 8 }}>live egress ·
        {' '}{feed.length} event{feed.length !== 1 && 's'}</div>
      <div className="net-feed-list grow-scroll">
        {feed.length === 0 && (
          <div className="dim center-pad">no egress yet — outbound requests the
            agent's code makes stream in here</div>
        )}
        {feed.map((e) => <FeedRow key={e._k} e={e} />)}
      </div>
    </div>
  )
}


export default function Network() {
  const [projects, setProjects] = useState([])
  const [filter, setFilter] = useState('')      // '' = all projects
  const [feed, setFeed] = useState([])
  const [pending, setPending] = useState([])
  const keyRef = useRef(0)

  useEffect(() => {
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {})
  }, [])

  // seed the feed, then subscribe to the live stream (global; filtered for
  // display client-side so switching the project filter never drops the socket)
  useEffect(() => {
    let live = true
    api('/api/egress/events?limit=200').then((r) => {
      const evs = (Array.isArray(r) ? r : r.events) || []
      // REST rows carry project_slug; the live stream carries project
      if (live) setFeed(evs.map((e) => (
        { ...e, project: e.project ?? e.project_slug, _k: ++keyRef.current })))
    }).catch(() => {})
    const stop = subscribeSse('/api/egress/stream', (ev) => {
      if (ev.type !== 'egress') return
      setFeed((f) => [{ ...ev, _k: ++keyRef.current }, ...f].slice(0, FEED_CAP))
    })
    return () => { live = false; stop() }
  }, [])

  // approval queue, re-scoped when the project filter changes
  useEffect(() => {
    const load = () => api(`/api/egress/pending${filter ? `?project=${encodeURIComponent(filter)}` : ''}`)
      .then((r) => setPending(r.pending || [])).catch(() => {})
    load()
    const t = setInterval(load, 10000)
    return () => clearInterval(t)
  }, [filter])

  function reloadPending() {
    api(`/api/egress/pending${filter ? `?project=${encodeURIComponent(filter)}` : ''}`)
      .then((r) => setPending(r.pending || [])).catch(() => {})
  }
  async function decide(id, verb) {
    try { await api(`/api/egress/pending/${id}/${verb}`, { method: 'POST' }); reloadPending() }
    catch (err) { window.alert(err.detail || String(err)) }
  }

  const shown = filter ? feed.filter((e) => e.project === filter) : feed

  return (
    <div className="net-page">
      <div className="net-head">
        <h2>Network</h2>
        <span className="run-dot running" title="live egress stream" />
        <span className="dim small">live egress</span>
        <span className="grow" />
        <label className="dim small">project&nbsp;
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">all projects</option>
            {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
          </select>
        </label>
      </div>

      <div className="net-body">
        <div className="net-feed">
          <div className="dim small">{shown.length} event{shown.length !== 1 && 's'}
            {' '}· newest first · capped at {FEED_CAP}</div>
          <div className="net-feed-list">
            {shown.length === 0 && (
              <div className="dim center-pad">no egress yet — the guest's outbound
                requests stream in here as they happen</div>
            )}
            {shown.map((e) => <FeedRow key={e._k} e={e} />)}
          </div>
        </div>

        <div className="net-side">
          <div className="sbx-card">
            <div className="sbx-sec-head"><h3>Host approvals</h3>
              <span className="dim small">{pending.length} waiting</span></div>
            <div className="dim small">hosts the guest keeps reaching for — approve to
              train the allowlist up, reject to keep it out</div>
            <ul className="staged-list rev-list">
              {pending.length === 0 && <li className="dim" style={{ cursor: 'default' }}>nothing waiting</li>}
              {pending.map((p) => (
                <li key={p.id}>
                  <span className="tag pending">{p.hit_count}×</span>
                  <span className="grow ellipsis mono" title={p.host}>{p.host}</span>
                  {!filter && p.project_slug && <span className="tag">{p.project_slug}</span>}
                  <button className="win-btn ok" title="approve" onClick={() => decide(p.id, 'approve')}>✓</button>
                  <button className="win-btn" title="reject" onClick={() => decide(p.id, 'reject')}>✕</button>
                </li>
              ))}
            </ul>
          </div>

          {filter ? (
            <>
              <PolicyEditor slug={filter} />
              <Grants slug={filter} />
            </>
          ) : (
            <div className="dim small net-hint">pick a project above to edit its egress
              policy and secret grants</div>
          )}
        </div>
      </div>
    </div>
  )
}
