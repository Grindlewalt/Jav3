import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, subscribeSse } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { human, tsShort } from '../format.js'
import { Link } from 'react-router-dom'
import { Button, EmptyState, Input, Select, Tag, Toggle } from '../components/index.js'
import { listProfiles } from '../boxes/api/profiles.js'
import {
  allowlist, getPolicy, promoteAuto, promoteToProfile, putPolicy, revokeAllow,
} from '../boxes/api/policy.js'
import { groupPolicy, parseHosts } from '../boxes/logic.js'

// The guest's network, read like a log: what is waiting on you, what was
// decided (and by whom — you, the allowlist, or the auto guesser), and what
// is standing on the allowlist, each entry revocable where it sits.
//
// Every string here — host, path, project, reason — is UNTRUSTED (it is guest
// traffic, or a model's one-line guess about it). It is only ever rendered as
// plain text nodes, never markup.

const FEED_CAP = 300
const GENERAL = '__general__'

// The operator's wording, verbatim — this is the whole disclaimer.
const AUTO_LABEL = 'Auto (test only — can make mistakes; you can leave it on)'

// egress_events.verdict -> what the row says, and the Tag that says it.
// Auto decisions share their manual twin's colour and add a dashed outline,
// so "who decided" reads without a legend.
const VERDICTS = {
  allow: { text: 'allowed', tone: 'done' },
  deny: { text: 'denied', tone: 'pending' },
  auto_allow: { text: 'auto-allowed', tone: 'done', auto: true },
  auto_deny: { text: 'auto-denied', tone: 'pending', auto: true },
  cut: { text: 'cut', tone: 'error' },
  // not a request: the queue approval that followed a deny, logged so the
  // deny isn't the last word on a host that has since been let through
  approved: { text: 'approved', tone: 'done' },
  reviewer_approved: { text: 'approved', tone: 'done', auto: true },   // dashed = the reviewer
}
const APPROVALS = new Set(['approved', 'reviewer_approved'])
const verdictOf = (v) => VERDICTS[v] || { text: v || '?', tone: 'pending' }

function VerdictTag({ verdict }) {
  const v = verdictOf(verdict)
  return <Tag tone={v.tone} className={`net-verdict${v.auto ? ' auto' : ''}`}>{v.text}</Tag>
}

// allowlist entry source -> Tag
const SOURCES = {
  seed: { text: 'built-in', tone: undefined },
  operator: { text: 'you', tone: 'done' },
  reviewer: { text: 'reviewer', tone: 'running' },
  auto: { text: 'auto', tone: 'pending' },
}

const projLabel = (slug, names) =>
  !slug || slug === GENERAL ? 'shared' : (names[slug] || slug)

// ---- data hooks -----------------------------------------------------------------

// The live egress feed. Seed from REST, then follow the stream. `project`
// scopes it: the panel wants only its own project's rows, so it filters at the
// source; the page holds everything and filters at render time, so switching
// its project picker never drops the socket.
function useEgressFeed(project, onEvent) {
  const [feed, setFeed] = useState([])
  const keyRef = useRef(0)
  const cb = useRef(onEvent)
  cb.current = onEvent
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
      // stamped on arrival (UTC, like the DB's) so the row has a time at all
      const row = { ...ev, created_at: new Date().toISOString(), _k: ++keyRef.current }
      setFeed((f) => [row, ...f].slice(0, FEED_CAP))
      cb.current?.(ev)
    })
    return () => { live = false; stop() }
  }, [project])
  return feed
}

// Poll a GET every 10s (and on demand). Returns [data, reload].
function usePoll(path, pick) {
  const [data, setData] = useState(null)
  const reload = useCallback(() => {
    if (!path) return
    api(path).then((r) => setData(pick(r))).catch(() => {})
  }, [path]) // eslint-disable-line
  useEffect(() => {
    setData(null)
    reload()
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [reload])
  return [data, reload]
}

const q = (project) => (project ? `?project=${encodeURIComponent(project)}` : '')

// ---- the top strip: project · Auto · counts -------------------------------------

function AutoToggle({ project, onChange }) {
  const [mode, reload] = usePoll(`/api/egress/auto${q(project)}`, (r) => r)
  async function flip(on) {
    try {
      await api('/api/egress/auto', {
        method: 'PUT',
        body: JSON.stringify({ project: project || null, mode: on ? 'on' : 'off' }) })
      reload(); onChange?.()
    } catch (err) { notifyError(err) }
  }
  const scope = !project ? 'default for every project'
    : mode && mode.project === null ? `following the default (${mode.global})`
      : 'this project only'
  return (
    <div className="net-auto">
      <Toggle checked={!!mode?.effective} disabled={!mode} label={AUTO_LABEL}
              onText={AUTO_LABEL} offText={AUTO_LABEL} onChange={flip} />
      <span className="dim small">{scope}</span>
    </div>
  )
}

function Counts({ project, tick }) {
  const [c, reload] = usePoll(`/api/egress/summary${q(project)}`, (r) => r)
  useEffect(() => { reload() }, [tick]) // eslint-disable-line
  const n = (k) => (c ? c[k] : '–')
  return (
    <div className="net-counts" title="distinct hosts in the last 24 hours; waiting is now">
      <span><b>{n('allowed')}</b> allowed</span>
      <span><b>{n('denied')}</b> denied</span>
      <span className={c?.waiting ? 'net-count-waiting' : ''}><b>{n('waiting')}</b> waiting</span>
    </div>
  )
}

// ---- waiting for you --------------------------------------------------------------

// Hosts the guest's code tried to reach that nobody has decided on. Allow
// trains the allowlist up (the project's own list, or the shared one for a
// project without its own); Deny keeps it out.
function Waiting({ project, names, showProject, lastTry, tick, onDecided }) {
  const [pending, reload] = usePoll(`/api/egress/pending${q(project)}`, (r) => r.pending || [])
  useEffect(() => { reload() }, [tick]) // eslint-disable-line
  async function decide(id, verb) {
    try {
      await api(`/api/egress/pending/${id}/${verb}`, { method: 'POST' })
      reload(); onDecided?.()
    } catch (err) { notifyError(err) }
  }
  const rows = pending || []
  return (
    <section className="net-sec">
      <div className="sbx-sec-head"><h3>Waiting for you</h3>
        <span className="sec-count">{rows.length}</span></div>
      {rows.length === 0 && <EmptyState>nothing waiting</EmptyState>}
      <ul className="net-list">
        {rows.map((p) => {
          const last = lastTry(p.project_slug, p.host)
          const tried = last?.method
            ? `${last.method}${last.path ? ` ${last.path}` : ''}` : 'connect'
          return (
            <li key={p.id} className="net-wait">
              <span className="net-host mono" title={p.host}>{p.host}</span>
              <span className="net-wait-meta">
                {showProject && <Tag>{projLabel(p.project_slug, names)}</Tag>}
                <span className="net-tried dim" title={tried}>
                  {p.hit_count}× · {tried}</span>
                {p.auto_verdict === 'unsure' && (
                  <Tag tone="pending" className="net-verdict auto"
                       title={p.auto_reason || ''}>auto: unsure</Tag>)}
                {p.triage_verdict === 'flag' && (
                  <Tag className="triage-flag" title={p.triage_reason || ''}>
                    ⚑ {p.triage_reason}</Tag>)}
              </span>
              <span className="net-actions">
                <Button onClick={() => decide(p.id, 'approve')}>Allow</Button>
                <Button variant="ghost" onClick={() => decide(p.id, 'reject')}>Deny</Button>
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

// ---- recent decisions ---------------------------------------------------------------

// A burst of identical requests (pip opening eight connections to one host)
// is ONE decision: consecutive rows with the same host, verdict and project
// fold into one line with a count and summed bytes.
function fold(feed) {
  const out = []
  for (const e of feed) {
    const prev = out[out.length - 1]
    if (prev && prev.host === e.host && prev.verdict === e.verdict
        && prev.project === e.project) {
      prev.n += 1
      prev.bytes_out += Number(e.bytes_out) || 0
      prev.bytes_in += Number(e.bytes_in) || 0
      continue
    }
    out.push({ ...e, n: 1, bytes_out: Number(e.bytes_out) || 0,
               bytes_in: Number(e.bytes_in) || 0 })
  }
  return out
}

function DecisionRow({ d, names, onAllow }) {
  const [open, setOpen] = useState(false)
  const req = d.method ? `${d.method}${d.path ? ` ${d.path}` : ''}` : ''
  return (
    <li className={`net-dec${open ? ' open' : ''}`}>
      <button type="button" className="net-dec-row" aria-expanded={open}
              title={d.reason || ''} onClick={() => setOpen(!open)}>
        <span className="net-dec-time dim">{tsShort(d.created_at) || 'now'}</span>
        <span className="net-dec-verdict"><VerdictTag verdict={d.verdict} /></span>
        <span className="net-host mono">{d.host}{d.n > 1 && (
          <span className="dim net-dec-n"> ×{d.n}</span>)}</span>
        <span className="net-dec-proj dim">{projLabel(d.project, names)}</span>
        <span className="net-dec-bytes dim" title="sent / received">
          {!APPROVALS.has(d.verdict) && <>↑{human(d.bytes_out)} ↓{human(d.bytes_in)}</>}</span>
      </button>
      {open && (
        <div className="net-dec-detail">
          {d.reason && <div>{d.reason}</div>}
          {req && <div className="mono dim ellipsis" title={req}>{req}</div>}
          {d.verdict === 'auto_deny' && (
            <div><Button variant="ghost" onClick={() => onAllow(d)}>
              Allow it anyway</Button></div>
          )}
        </div>
      )}
    </li>
  )
}

function Decisions({ feed, names, onChanged }) {
  const rows = useMemo(() => fold(feed), [feed])
  async function allowAnyway(d) {
    try {
      await api('/api/egress/allow', { method: 'POST',
        body: JSON.stringify({ project: d.project || '', host: d.host }) })
      onChanged?.()
    } catch (err) { notifyError(err) }
  }
  return (
    <section className="net-sec">
      <div className="sbx-sec-head"><h3>Recent decisions</h3>
        <span className="dim small">newest first · tap a row for why</span></div>
      {rows.length === 0 && (
        <EmptyState>no traffic yet — the guest&#39;s outbound requests
          appear here as they happen</EmptyState>
      )}
      {rows.length > 0 && (
        <ul className="net-list net-dec-list">
          {rows.map((d) => <DecisionRow key={d._k} d={d} names={names}
                                        onAllow={allowAnyway} />)}
        </ul>
      )}
    </section>
  )
}

// ---- allow and deny, by project, then the profile baselines ----------------------
// Every approval writes the project's OWN list now (DESIGN-BOXES (c)); the
// profile's lists are the shared baseline under it, edited by the operator
// only — here by "→ profile" (promote), or on the Profiles tab. Deny beats
// allow at every level.

function PolicyLists({ project, names, projects, tick, onChanged }) {
  const ask = useAsk()
  const [groups, setGroups] = useState(null)
  const reload = useCallback(() => { allowlist().then(setGroups).catch(() => {}) }, [])
  useEffect(() => {
    const t = setInterval(reload, 10000)
    return () => clearInterval(t)
  }, [reload])
  const [profiles, setProfiles] = useState([])
  const loadProfiles = useCallback(() => {
    listProfiles().then(setProfiles).catch(() => setProfiles([]))
  }, [])
  useEffect(() => { reload(); loadProfiles() }, [tick]) // eslint-disable-line
  const { projectGroups, profileGroups } = useMemo(() => groupPolicy({
    groups: groups || [], profiles, filter: project,
    projects: projects.length ? projects : Object.entries(names).map(([slug, name]) => ({ slug, name })),
  }), [groups, profiles, project, projects, names])
  const refresh = () => { reload(); loadProfiles(); onChanged?.() }

  async function revoke(slug, e) {
    const ok = await ask.confirm(`Revoke ${e.host}?`, {
      body: `It comes off ${projLabel(slug, names)}'s own list; the next attempt is judged by `
        + 'its profile (and waits for you if the profile does not allow it).',
      confirmLabel: 'Revoke', danger: true })
    if (!ok) return
    try {
      await revokeAllow(e.source === 'auto' ? { id: e.id } : { project: slug, host: e.host })
      refresh()
    } catch (err) { notifyError(err) }
  }
  async function keep(e) {
    try { await promoteAuto(e.id); refresh() } catch (err) { notifyError(err) }
  }
  async function editDeny(slug, host, add) {
    try {
      const pol = await getPolicy(slug)
      const deny = add ? [...new Set([...pol.project_deny, host])]
        : pol.project_deny.filter((h) => h !== host)
      const allow = add ? pol.project_allow.filter((h) => h !== host) : pol.project_allow
      await putPolicy(slug, allow, deny)
      refresh()
    } catch (err) { notifyError(err) }
  }
  async function addDeny(slug) {
    const host = await ask.prompt(`Deny a host for ${projLabel(slug, names)}`, '',
                                  { confirmLabel: 'Deny' })
    if (!host) return
    editDeny(slug, host.trim().toLowerCase(), true)
  }
  async function promote(g, host, kind) {
    const prof = profiles.find((p) => p.id === g.profile?.id)
    if (!prof) { notifyError(new Error('this project has no profile to promote to yet')); return }
    const others = (prof.projects || []).filter((s) => s !== g.slug)
    const ok = await ask.confirm(`Move ${host} to ${prof.name}'s ${kind} list?`, {
      body: `It leaves ${g.name}'s own list and becomes part of the ${prof.name} baseline: `
        + (others.length ? `${others.join(', ')} ${kind === 'deny' ? 'are denied' : 'are allowed'} it too.`
          : 'no other project uses that profile today, but any project moved onto it will be.'),
      confirmLabel: 'Promote', danger: kind === 'allow' && others.length > 0 })
    if (!ok) return
    try { await promoteToProfile(prof, g.slug, host, kind); refresh() } catch (err) { notifyError(err) }
  }

  return (
    <section className="net-sec">
      <div className="sbx-sec-head"><h3>Allow &amp; deny — by project</h3></div>
      {projectGroups.length === 0 && <EmptyState>no project has its own list yet</EmptyState>}
      {projectGroups.map((g) => (
        <div key={g.slug} className="net-group">
          <div className="net-group-head">
            {g.name}
            {g.profile && <> <Tag title="its profile: the baseline under this list">
              {g.profile.name}</Tag></>}
            <span className="dim small"> · {g.allow.length} allowed · {g.deny.length} denied</span>
            <Button variant="link" onClick={() => addDeny(g.slug)}>+ deny a host</Button>
          </div>
          <ul className="net-list">
            {g.allow.map((e) => {
              const src = SOURCES[e.source] || { text: e.source || 'you' }
              return (
                <li key={`a:${e.source}:${e.id ?? e.host}`} className="net-allow">
                  <span className="net-host mono" title={e.host}>{e.host}</span>
                  <span className="net-allow-meta">
                    <Tag tone={src.tone} className={e.source === 'auto' ? 'net-verdict auto' : ''}
                         title={e.reason || ''}>{src.text}</Tag>
                    {e.source === 'auto' && (
                      <span className="dim small" title={e.reason || ''}>until {tsShort(e.expires_at)}</span>)}
                  </span>
                  <span className="net-actions">
                    {e.source === 'auto' ? (
                      <Button variant="ghost" title="keep it: move it onto the list"
                              onClick={() => keep(e)}>Keep</Button>
                    ) : g.profile && (
                      <Button variant="ghost" title={`move it to ${g.profile.name}'s allow list`}
                              onClick={() => promote(g, e.host, 'allow')}>→ profile</Button>
                    )}
                    <Button variant="ghost" danger onClick={() => revoke(g.slug, e)}>Revoke</Button>
                  </span>
                </li>
              )
            })}
            {g.deny.map((e) => (
              <li key={`d:${e.host}`} className="net-allow net-deny">
                <span className="net-host mono" title={e.host}>{e.host}</span>
                <span className="net-allow-meta"><Tag tone="error">denied</Tag></span>
                <span className="net-actions">
                  {g.profile && (
                    <Button variant="ghost" title={`move it to ${g.profile.name}'s deny list`}
                            onClick={() => promote(g, e.host, 'deny')}>→ profile</Button>)}
                  <Button variant="ghost" onClick={() => editDeny(g.slug, e.host, false)}>Remove</Button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}

      <div className="sbx-sec-head"><h3>Profile baselines</h3>
        <Link className="small" to="/security/profiles">edit profiles →</Link></div>
      {profileGroups.length === 0 && <EmptyState>no profiles on this server yet</EmptyState>}
      {profileGroups.map((p) => (
        <div key={p.id} className="net-group">
          <div className="net-group-head">
            {p.name}
            <span className="dim small">
              {' · '}{p.network_off ? 'network off'
                : `unlisted hosts ${p.default_verdict === 'allow' ? 'allowed' : 'denied'}`}
              {p.projects.length ? ` · ${p.projects.map((s) => projLabel(s, names)).join(', ')}` : ''}
            </span>
          </div>
          {p.allow.length === 0 && p.deny.length === 0 && <EmptyState>empty</EmptyState>}
          <div className="bx-hosts">
            {p.allow.map((h) => <Tag key={`a${h}`} tone="done" className="mono">{h}</Tag>)}
            {p.deny.map((h) => <Tag key={`d${h}`} tone="error" className="mono" title="denied">✕ {h}</Tag>)}
          </div>
        </div>
      ))}
    </section>
  )
}

// ---- per-project egress policy editor ---------------------------------------
// The project's own lists over its profile's baseline. What the proxy applies
// is the effective pair (decision order: cut, project deny, profile deny,
// project allow, profile allow, live auto-allow, the profile's default).
function PolicyEditor({ slug }) {
  const [pol, setPol] = useState(null)
  const [allowText, setAllowText] = useState('')
  const [denyText, setDenyText] = useState('')
  const [status, setStatus] = useState('')
  const [saving, setSaving] = useState(false)

  function load() {
    getPolicy(slug).then((p) => {
      setPol(p); setAllowText(p.project_allow.join('\n')); setDenyText(p.project_deny.join('\n'))
    }).catch(() => setPol(null))
  }
  useEffect(() => { load() }, [slug]) // eslint-disable-line

  async function save() {
    setSaving(true)
    try {
      await putPolicy(slug, parseHosts(allowText), parseHosts(denyText))
      setStatus('saved'); setTimeout(() => setStatus(''), 1500); load()
    } catch (err) { notifyError(err) }
    setSaving(false)
  }

  if (!pol) return null
  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Egress policy</h3>
        <span className="dim small">{status}</span></div>
      <div className="net-policy">
        <div className="small">
          profile <b>{pol.profile?.name || 'Default'}</b>
          {pol.profile?.default && (
            <span className="dim"> · unlisted hosts {pol.profile.default === 'allow' ? 'allowed' : 'denied'}</span>)}
          {' '}<Link to="/security/profiles" className="small">change…</Link>
        </div>
        <div className="bx-two">
          <Input textarea label="This project allows (one per line)" className="md-editor" rows={4}
                 spellCheck={false} value={allowText} onChange={(e) => setAllowText(e.target.value)} />
          <Input textarea label="This project denies — beats every allow" className="md-editor" rows={4}
                 spellCheck={false} value={denyText} onChange={(e) => setDenyText(e.target.value)} />
        </div>
        <div className="row">
          <span className="grow" />
          <Button disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save policy'}</Button>
        </div>
        {pol.effective_allow.length > 0 && (
          <div className="dim small">effective allow: {pol.effective_allow.join(', ')}</div>)}
        {pol.effective_deny.length > 0 && (
          <div className="dim small">effective deny: {pol.effective_deny.join(', ')}</div>)}
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


// latest feed row per (project, host) — what the guest was doing when it asked
function useLastTry(feed) {
  const idx = useMemo(() => {
    const m = new Map()
    for (const e of feed) {
      const k = `${e.project || GENERAL}|${e.host}`
      if (!m.has(k) && e.method) m.set(k, e)
    }
    return m
  }, [feed])
  return useCallback((project, host) => idx.get(`${project || GENERAL}|${host}`), [idx])
}

// A tick that bumps on every decision the live feed reports (or one made here),
// so the counts, the queue and the allowlist refresh at once instead of on
// their next 10s poll.
// Coalesced: a guest retrying a denied host fires a burst, and each bump is
// three fetches.
function useTick() {
  const [tick, setTick] = useState(0)
  const timer = useRef(null)
  const bump = useCallback(() => {
    if (timer.current) return
    timer.current = setTimeout(() => { timer.current = null; setTick((t) => t + 1) }, 800)
  }, [])
  useEffect(() => () => clearTimeout(timer.current), [])
  return [tick, bump]
}
const decisive = (ev) => ev.verdict !== 'allow'

// Compact, project-scoped egress view for a Workspace panel: the same three
// sections as the page, for this one project, plus its policy and grants.
export function NetworkPanel({ slug }) {
  const [tick, bump] = useTick()
  const feed = useEgressFeed(slug, (ev) => decisive(ev) && bump())
  const lastTry = useLastTry(feed)
  return (
    <div className="pane-col net-panel">
      <div className="net-top">
        <AutoToggle project={slug} onChange={bump} />
        <Counts project={slug} tick={tick} />
      </div>
      <Waiting project={slug} names={{}} lastTry={lastTry} tick={tick} onDecided={bump} />
      <Decisions feed={feed.slice(0, 60)} names={{}} onChanged={bump} />
      <PolicyLists project={slug} names={{}} projects={[]} tick={tick} onChanged={bump} />
      <PolicyEditor slug={slug} />
      <Grants slug={slug} />
    </div>
  )
}


export default function Network() {
  const [projects, setProjects] = useState([])
  const [filter, setFilter] = useState('')      // '' = all projects
  const [tick, bump] = useTick()
  // the page holds every project's events and narrows at render, so changing
  // the filter never tears down the stream
  const feed = useEgressFeed('', (ev) => decisive(ev) && bump())
  const lastTry = useLastTry(feed)

  useEffect(() => {
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {})
  }, [])
  const names = useMemo(
    () => Object.fromEntries(projects.map((p) => [p.slug, p.name])), [projects])

  const shown = filter ? feed.filter((e) => e.project === filter) : feed

  // No heading or page padding of its own: this renders as the Network tab of
  // the Security layout, which owns the title, the tab strip and the insets.
  return (
    <div className="net-view">
      <div className="net-top">
        <Select aria-label="project" value={filter}
                onChange={(e) => setFilter(e.target.value)}
                options={[{ value: '', label: 'All projects' },
                  ...projects.map((p) => ({ value: p.slug, label: p.name }))]} />
        <AutoToggle project={filter} onChange={bump} />
        <Counts project={filter} tick={tick} />
      </div>

      <Waiting project={filter} names={names} showProject={!filter}
               lastTry={lastTry} tick={tick} onDecided={bump} />
      <Decisions feed={shown} names={names} onChanged={bump} />
      <PolicyLists project={filter} names={names} projects={projects} tick={tick} onChanged={bump} />

      {filter ? (
        <details className="net-sec net-more">
          <summary>Policy and secret grants for {names[filter] || filter}</summary>
          <PolicyEditor slug={filter} />
          <Grants slug={filter} />
        </details>
      ) : (
        <div className="dim small">pick a project above to edit its egress
          policy and secret grants</div>
      )}
    </div>
  )
}
