import { useEffect, useRef, useState } from 'react'
import { api, subscribeSse } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { human } from '../format.js'
import { Button, EmptyState, Input, Select, Toggle } from '../components/index.js'

// The guest's live egress: a scrolling feed of every outbound request the
// sandbox made, with a verdict chip (allow / deny / cut), an approval queue for
// hosts the guest keeps trying to reach ("training the allowlist up"), and a
// per-project policy + secret-grant editor.
//
// Every string here — host, path, project, reason — is UNTRUSTED (it is guest
// traffic). It is only ever rendered as plain text nodes, never markup.

const FEED_CAP = 300

// allow -> green, cut -> red, everything else (deny / anomaly) -> amber
const verdictClass = (v) => (v === 'allow' ? 'allow' : v === 'cut' ? 'cut' : 'deny')

// One line on a desktop. On a phone, two: what was reached and how much
// (verdict, host, bytes), then the particulars (project, request, reason) —
// .egr-meta is `display: contents` until then, so it costs nothing wide.
function FeedRow({ e }) {
  const cls = verdictClass(e.verdict)
  return (
    <div className={`egr-row ${cls === 'allow' ? '' : cls}`}>
      <span className={`egr-chip ${cls}`}>{e.verdict}</span>
      <span className="egr-host" title={e.host}>{e.host}</span>
      <span className="egr-meta">
        {e.project && <span className="tag">{e.project}</span>}
        <span className="egr-path" title={`${e.method || ''} ${e.path || ''}`}>
          {e.method ? `${e.method} ` : ''}{e.path}</span>
        {e.reason && <span className="egr-reason" title={e.reason}>{e.reason}</span>}
      </span>
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
    } catch (err) { notifyError(err) }
    setSaving(false)
  }

  if (!pol) return null
  return (
    <div className="sbx-card">
      {/* the project is named by the picker above (or is the panel's own) —
          repeated here in the uppercase head it hyphen-broke across lines */}
      <div className="sbx-sec-head"><h3>Egress policy</h3>
        <span className="dim small">{status}</span></div>
      <div className="net-policy">
        <Select label="Mode" value={pol.mode || 'allowlist'}
                onChange={(e) => setPol({ ...pol, mode: e.target.value })}
                options={[
                  { value: 'allowlist', label: 'Allowlist — only listed hosts' },
                  { value: 'denylist', label: 'Denylist — all but listed hosts' },
                  { value: 'denyall', label: 'Deny all — no egress' }]} />
        <Toggle checked={!!pol.inherit_general} label="Inherit the general allowlist"
                onText="Inherit the general allowlist"
                offText="Inherit the general allowlist"
                onChange={(on) => setPol({ ...pol, inherit_general: on })} />
        <Input textarea label="Hosts (one per line)" className="md-editor" rows={4}
               spellCheck={false} value={hostsText}
               onChange={(e) => setHostsText(e.target.value)} />
        <div className="row">
          <span className="dim small grow">
            effective: {pol.mode}{pol.source ? ` · source: ${pol.source}` : ''}</span>
          <Button disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save policy'}</Button>
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
  const ask = useAsk()
  function load() {
    api(`/api/egress/grants/${slug}`).then((r) => setGrants(r.grants || [])).catch(() => setGrants([]))
  }
  useEffect(() => { load() }, [slug]) // eslint-disable-line

  async function set(secret, statusVal) {
    try {
      await api(`/api/egress/grants/${slug}`, {
        method: 'POST', body: JSON.stringify({ secret, status: statusVal }) })
      load()
    } catch (err) { notifyError(err) }
  }
  async function add() {
    const name = await ask.prompt('Secret name to grant to this project', '',
                                  { confirmLabel: 'Grant' })
    if (!name) return
    set(name.trim(), 'granted')
  }

  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Secret grants</h3>
        <Button variant="ghost" onClick={add}>Grant secret</Button></div>
      <ul className="staged-list rev-list">
        {grants.length === 0 && <EmptyState as="li">none granted</EmptyState>}
        {grants.map((g) => {
          const granted = g.status === 'granted'
          return (
            <li key={g.secret_name}>
              <span className={`tag ${granted ? 'done' : ''}`}>{g.status}</span>
              <span className="grow ellipsis mono">{g.secret_name}</span>
              <Button variant="ghost" danger={granted}
                      onClick={() => set(g.secret_name, granted ? 'revoked' : 'granted')}>
                {granted ? 'Revoke' : 'Grant'}</Button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ---- the live egress feed ----------------------------------------------------
// Seed from REST, then follow the stream. `project` scopes it: the panel wants
// only its own project's rows, so it filters at the source; the page holds
// everything and filters at render time, so switching its project picker never
// drops the socket.
function useEgressFeed(project) {
  const [feed, setFeed] = useState([])
  const keyRef = useRef(0)
  useEffect(() => {
    let live = true
    api('/api/egress/events?limit=200').then((r) => {
      const evs = (Array.isArray(r) ? r : r.events) || []
      // REST rows carry project_slug; the live stream carries project
      const rows = evs.map((e) => ({ ...e, project: e.project ?? e.project_slug }))
      const kept = project ? rows.filter((e) => e.project === project) : rows
      if (live) setFeed(kept.map((e) => ({ ...e, _k: ++keyRef.current })))
    }).catch(() => {})
    const stop = subscribeSse('/api/egress/stream', (ev) => {
      if (ev.type !== 'egress') return
      if (project && ev.project !== project) return
      setFeed((f) => [{ ...ev, _k: ++keyRef.current }, ...f].slice(0, FEED_CAP))
    })
    return () => { live = false; stop() }
  }, [project])
  return feed
}

// ---- host approvals ----------------------------------------------------------
// The hosts the guest's code tried to reach and could not, with approve (which
// trains the allowlist up) and reject. This card was written out TWICE in this
// file — once in the panel, once on the page — each with its own poll effect and
// its own `decide`, 110 lines apart. One component, two mounts.
//
// `project` scopes the queue ('' = every project). The blurb differs between the
// two mounts and stays a prop rather than being picked for them, and only the
// unscoped page has anything to say with the project tag.
function HostApprovals({ project = '', showProject = false, children }) {
  const [pending, setPending] = useState([])
  const reload = () =>
    api(`/api/egress/pending${project ? `?project=${encodeURIComponent(project)}` : ''}`)
      .then((r) => setPending(r.pending || [])).catch(() => {})
  useEffect(() => {
    reload()
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [project]) // eslint-disable-line
  async function decide(id, verb) {
    try { await api(`/api/egress/pending/${id}/${verb}`, { method: 'POST' }); reload() }
    catch (err) { notifyError(err) }
  }
  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Host approvals</h3>
        <span className="dim small">{pending.length} waiting</span></div>
      <div className="dim small">{children}</div>
      <ul className="staged-list rev-list">
        {pending.length === 0 && <EmptyState as="li">nothing waiting</EmptyState>}
        {pending.map((p) => (
          <li key={p.id}>
            <span className="tag pending">{p.hit_count}×</span>
            <span className="grow ellipsis mono" title={p.host}>{p.host}</span>
            {p.triage_verdict === 'flag' && (
              <span className="tag triage-flag" title={p.triage_reason}>⚑ {p.triage_reason}</span>)}
            {showProject && p.project_slug && <span className="tag">{p.project_slug}</span>}
            <button className="win-btn ok" title="approve" onClick={() => decide(p.id, 'approve')}>✓</button>
            <button className="win-btn" title="reject" onClick={() => decide(p.id, 'reject')}>✕</button>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Compact, project-scoped egress view for a Workspace panel: the live feed
// filtered to this project, its host-approval queue, policy + grants. Same data
// and endpoints as the full Network page, no project picker.
export function NetworkPanel({ slug }) {
  const feed = useEgressFeed(slug)

  return (
    <div className="pane-col net-panel">
      <HostApprovals project={slug}>
        hosts the agent&#39;s code tried to reach — approve to
        let it through (trains the allowlist), reject to keep it out
      </HostApprovals>
      <PolicyEditor slug={slug} />
      <Grants slug={slug} />
      <div className="dim small" style={{ marginTop: 8 }}>live egress ·
        {' '}{feed.length} event{feed.length !== 1 && 's'}</div>
      <div className="net-feed-list grow-scroll">
        {feed.length === 0 && (
          <EmptyState pad>no egress yet — outbound requests the
            agent&#39;s code makes stream in here</EmptyState>
        )}
        {feed.map((e) => <FeedRow key={e._k} e={e} />)}
      </div>
    </div>
  )
}


export default function Network() {
  const [projects, setProjects] = useState([])
  const [filter, setFilter] = useState('')      // '' = all projects
  // the page holds every project's events and narrows at render, so changing
  // the filter never tears down the stream
  const feed = useEgressFeed('')

  useEffect(() => {
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {})
  }, [])

  const shown = filter ? feed.filter((e) => e.project === filter) : feed

  // No heading or page padding of its own: this renders as the Network tab of
  // the Review layout, which owns the title, the tab strip and the insets.
  return (
    <div className="net-view">
      <div className="net-head">
        <span className="run-dot running" title="live egress stream" />
        <span className="dim small">live egress</span>
        <span className="grow" />
        <Select aria-label="project" value={filter}
                onChange={(e) => setFilter(e.target.value)}
                options={[{ value: '', label: 'all projects' },
                  ...projects.map((p) => ({ value: p.slug, label: p.name }))]} />
      </div>

      <div className="net-body">
        <div className="net-feed">
          <div className="dim small">{shown.length} event{shown.length !== 1 && 's'}
            {' '}· newest first · capped at {FEED_CAP}</div>
          <div className="net-feed-list">
            {shown.length === 0 && (
              <EmptyState pad>no egress yet — the guest&#39;s outbound
                requests stream in here as they happen</EmptyState>
            )}
            {shown.map((e) => <FeedRow key={e._k} e={e} />)}
          </div>
        </div>

        <div className="net-side">
          <HostApprovals project={filter} showProject={!filter}>
            hosts the guest keeps reaching for — approve to
            train the allowlist up, reject to keep it out
          </HostApprovals>

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
