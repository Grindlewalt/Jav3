import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, subscribeSse } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { human, tsShort } from '../format.js'
import { Link } from 'react-router-dom'
import { Button, EmptyState, Input, Select, Tag, Toggle } from '../components/index.js'
import { listProfiles } from '../boxes/api/profiles.js'
import {
  allowHost, allowlist, approvePending, getLan, getPolicy, promoteAuto, promoteToProfile,
  putLan, putPolicy, rejectPending, revokeAllow,
} from '../boxes/api/policy.js'
import {
  GENERAL, IMAGE_BUILD, needsProject, newSitesText, parseHosts, POLICY_FROM, projectLabel,
  projectPolicy,
} from '../boxes/logic.js'

// The guest's network, read like a log: what is waiting on you, what was
// decided (and by whom — you, the allowlist, or the auto guesser), and what
// is standing on the allowlist, each entry revocable where it sits.
//
// Every string here — host, path, project, reason — is UNTRUSTED (it is guest
// traffic, or a model's one-line guess about it). It is only ever rendered as
// plain text nodes, never markup.

const FEED_CAP = 300

// The operator's wording, verbatim — this is the whole disclaimer.
const AUTO_LABEL = 'Auto (test only — can make mistakes; you can leave it on)'

// egress_events.verdict -> what the row says, and the Tag that says it. Three
// words only: allowed, blocked, cut. Auto decisions keep their manual twin's
// colour, add a dashed outline and a small "auto"; a queue approval (logged
// so a block isn't the last word on a host since let through) reads
// "allowed" with a note saying who approved it.
const CUT_TIP = 'Cut by the anomaly guard: an allowed site looked like data leaving '
  + '(a random-looking name, a traffic spike, or clockwork-regular connections). '
  + 'The proxy stopped it there and refuses every later request to that site.'
const VERDICTS = {
  allow: { text: 'allowed', tone: 'done' },
  deny: { text: 'blocked', tone: 'pending' },
  auto_allow: { text: 'allowed', tone: 'done', auto: true },
  auto_deny: { text: 'blocked', tone: 'pending', auto: true },
  cut: { text: 'cut', tone: 'error', tip: CUT_TIP },
  approved: { text: 'allowed', tone: 'done', note: 'approved by you' },
  reviewer_approved: { text: 'allowed', tone: 'done', dashed: true, note: 'by the reviewer' },
}
const APPROVALS = new Set(['approved', 'reviewer_approved'])
const verdictOf = (v) => VERDICTS[v] || { text: v || '?', tone: 'pending' }

function VerdictTag({ verdict }) {
  const v = verdictOf(verdict)
  return (
    <Tag tone={v.tone} className={`net-verdict${v.auto || v.dashed ? ' auto' : ''}`}
         title={v.tip || (v.auto ? 'decided by auto mode' : v.note) || undefined}>
      {v.text}{v.auto && <span className="net-auto-mark"> auto</span>}
    </Tag>
  )
}

const projLabel = (slug, names) => projectLabel(slug, names)

// An unattributed row (shared box, no turn) goes on a PROJECT's list: the
// operator names which. Resolves a slug, or null when cancelled.
function usePickProject() {
  const ask = useAsk()
  return useCallback(async (host) => {
    let slugs = []
    try { slugs = ((await api('/api/projects')).projects || []).map((p) => p.slug) } catch { /* typed */ }
    slugs = slugs.filter((x) => !x.startsWith('__'))
    const got = await ask.prompt(
      `${host} came from no project. Whose list should it go on?`
        + (slugs.length ? ` (${slugs.join(', ')})` : ''),
      slugs.length === 1 ? slugs[0] : '', { confirmLabel: 'Allow' })
    const slug = (got || '').trim()
    if (!slug) return null
    if (slugs.length && !slugs.includes(slug)) {
      notifyError(new Error(`no project "${slug}"`))
      return null
    }
    return slug
  }, [ask])
}

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
      <span><b>{n('denied')}</b> blocked</span>
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
  const pick = usePickProject()
  async function decide(p, verb) {
    try {
      if (verb === 'approve') {
        let proj = null
        if (needsProject(p)) {
          proj = project || await pick(p.host)
          if (!proj) return
        }
        await approvePending(p.id, proj)
      } else {
        await rejectPending(p.id)
      }
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
                {(showProject || needsProject(p)) && (
                  <Tag tone={needsProject(p) ? 'pending' : undefined}>{projLabel(p.project_slug, names)}</Tag>)}
                {p.box_id && <Tag title="the box it came from">{p.box_id}</Tag>}
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
                <Button onClick={() => decide(p, 'approve')}
                        title={needsProject(p) ? 'unattributed: you pick the project whose list it joins' : undefined}>
                  {needsProject(p) && !project ? 'Allow for…' : 'Allow'}</Button>
                <Button variant="ghost" onClick={() => decide(p, 'reject')}>Deny</Button>
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
        && prev.project === e.project && (prev.box_id || null) === (e.box_id || null)) {
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
        <span className="net-dec-bytes dim" title={APPROVALS.has(d.verdict) ? undefined : 'sent / received'}>
          {APPROVALS.has(d.verdict) ? verdictOf(d.verdict).note
            : <>↑{human(d.bytes_out)} ↓{human(d.bytes_in)}</>}</span>
      </button>
      {open && (
        <div className="net-dec-detail">
          {d.reason && <div>{d.reason}</div>}
          {d.verdict === 'cut' && <div className="dim">{CUT_TIP}</div>}
          {req && <div className="mono dim ellipsis" title={req}>{req}</div>}
          {(d.box_id || d.service_id) && (
            <div className="dim small">
              {d.box_id ? `box ${d.box_id}` : ''}{d.box_id && d.service_id ? ' · ' : ''}
              {d.service_id ? `service #${d.service_id}` : ''}</div>)}
          {d.verdict === 'auto_deny' && d.project !== IMAGE_BUILD && (
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
  const pick = usePickProject()
  async function allowAnyway(d) {
    try {
      const proj = needsProject(d) ? await pick(d.host) : d.project
      if (!proj) return
      await allowHost(proj, d.host)
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

// ---- always allow / always block, one view per project -----------------------
// Each project with its profile and the profile's new-sites rule, then the
// lists that actually apply to it: the profile's and the project's own,
// merged, each entry marked with where it lives. Project entries are edited
// here (or in the project's own lists below); profile entries on the profile.
// Every approval writes the project's OWN list (DESIGN-BOXES (c)).

// backend/egress.py decide(), in one sentence
const ORDER = 'How a site is judged: a cut site, or a profile with the network off, is '
  + 'always blocked; otherwise any "always block" entry wins; then any "always allow" '
  + "entry (or a live auto allow) lets it through; any other site follows the profile's "
  + 'new-sites rule.'

// allow-entry source (who put it there) -> a small dim word
const WHO = { seed: 'built-in', reviewer: 'reviewer', operator: '' }

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
  const rows = useMemo(() => projectPolicy({
    groups: groups || [], profiles, filter: project,
    projects: projects.length ? projects : Object.entries(names).map(([slug, name]) => ({ slug, name })),
  }), [groups, profiles, project, projects, names])
  const refresh = () => { reload(); loadProfiles(); onChanged?.() }

  // project and auto entries only: the revoke route keys on the project slug
  async function revoke(r, e, list = 'allow') {
    const ok = await ask.confirm(`Remove ${e.host}?`, {
      body: list === 'deny'
        ? `It comes off ${r.name}'s always-block list; it is judged by what is left.`
        : `It comes off ${r.name}'s always-allow list; the next attempt is judged by what is left.`,
      confirmLabel: 'Remove', danger: true })
    if (!ok) return
    try {
      await revokeAllow(e.from === 'auto' && e.id != null
        ? { project: r.slug, host: e.host, id: e.id }
        : { project: r.slug, host: e.host, list })
      refresh()
    } catch (err) { notifyError(err) }
  }
  async function keep(e) {
    try { await promoteAuto(e.id); refresh() } catch (err) { notifyError(err) }
  }
  async function addBlock(r) {
    const host = await ask.prompt(`Always block a site for ${r.name}`, '', { confirmLabel: 'Block' })
    if (!host) return
    try {
      const pol = await getPolicy(r.slug)
      await putPolicy(r.slug, undefined, [...new Set([...pol.project_deny, host.trim().toLowerCase()])])
      refresh()
    } catch (err) { notifyError(err) }
  }
  async function promote(r, host, list) {
    const prof = profiles.find((p) => p.id === r.profile.id) || r.profile
    const others = (prof.projects || []).filter((x) => x !== r.slug)
    const what = list === 'deny' ? 'always-block' : 'always-allow'
    const ok = await ask.confirm(`Move ${host} to the ${prof.name} profile's ${what} list?`, {
      body: `It leaves ${r.name}'s own list and applies to every project on ${prof.name}: `
        + (others.length ? `${others.join(', ')} ${list === 'deny' ? 'block' : 'allow'} it too.`
          : 'no other project uses that profile today, but any project moved onto it will.'),
      confirmLabel: 'Move', danger: list === 'allow' && others.length > 0 })
    if (!ok) return
    // profile_id null: the project's own profile, resolved by the host
    try { await promoteToProfile(r.slug, host, { list }); refresh() } catch (err) { notifyError(err) }
  }

  const fromTag = (e) => {
    const f = POLICY_FROM[e.from] || { text: e.from }
    return (
      <Tag className={e.from === 'auto' ? 'net-verdict auto' : ''}
           tone={e.from === 'auto' ? 'pending' : e.from === 'project' ? 'done' : undefined}
           title={e.from === 'auto' ? (e.reason || f.title) : f.title}>{f.text}</Tag>
    )
  }
  const editProfile = <Link className="small" to="/security/profiles">edit profile</Link>
  const allowRow = (r, e) => (
    <li key={`a:${e.from}:${e.id ?? e.host}`} className={`net-allow${e.blocked ? ' net-overruled' : ''}`}>
      <span className="net-host mono" title={e.host}>{e.host}</span>
      <span className="net-allow-meta">
        {fromTag(e)}
        {WHO[e.source] && <span className="dim small">{WHO[e.source]}</span>}
        {e.from === 'auto' && (
          <span className="dim small" title={e.reason || ''}>until {tsShort(e.expires_at)}</span>)}
        {e.blocked && <span className="dim small">blocked wins</span>}
      </span>
      <span className="net-actions">
        {e.from === 'auto' && <>
          <Button variant="ghost" title="keep it: move it onto the project's list"
                  onClick={() => keep(e)}>Keep</Button>
          <Button variant="ghost" danger onClick={() => revoke(r, e)}>Revoke</Button></>}
        {e.from === 'project' && <>
          <Button variant="ghost" title={`move it to the ${r.profile.name} profile's always-allow list`}
                  onClick={() => promote(r, e.host, 'allow')}>→ profile</Button>
          <Button variant="ghost" danger onClick={() => revoke(r, e)}>Remove</Button></>}
        {(e.from === 'profile' || e.from === 'general') && e.source !== 'seed' && editProfile}
      </span>
    </li>
  )
  const blockRow = (r, e) => (
    <li key={`d:${e.from}:${e.host}`} className="net-allow net-deny">
      <span className="net-host mono" title={e.host}>{e.host}</span>
      <span className="net-allow-meta"><Tag tone="error">blocked</Tag>{fromTag(e)}</span>
      <span className="net-actions">
        {e.from === 'project' ? <>
          <Button variant="ghost" title={`move it to the ${r.profile.name} profile's always-block list`}
                  onClick={() => promote(r, e.host, 'deny')}>→ profile</Button>
          <Button variant="ghost" onClick={() => revoke(r, e, 'deny')}>Remove</Button></>
          : editProfile}
      </span>
    </li>
  )

  return (
    <section className="net-sec">
      <div className="sbx-sec-head"><h3>Always allow &amp; always block</h3>
        <Link className="small" to="/security/profiles">profiles →</Link></div>
      <div className="dim small net-order">{ORDER}</div>
      {groups && rows.length === 0 && <EmptyState>no projects yet</EmptyState>}
      {rows.map((r) => (
        <div key={r.slug} className="net-group">
          <div className="net-group-head">
            {r.name}
            {' '}<Tag title="this project's profile">
              {r.profile.isDefault ? 'Default profile' : `${r.profile.name} profile`}</Tag>
            <span className="dim small"> · new sites: {newSitesText(r.profile)}
              {' · '}{r.allow.length} always allowed · {r.block.length} blocked</span>
            <Button variant="link" onClick={() => addBlock(r)}>+ block a site</Button>
          </div>
          {r.allow.length === 0 && r.block.length === 0 && (
            <div className="dim small">nothing listed: every site follows the new-sites rule</div>)}
          <ul className="net-list">
            {r.block.map((e) => blockRow(r, e))}
            {r.allow.map((e) => allowRow(r, e))}
          </ul>
        </div>
      ))}
    </section>
  )
}

// ---- per-project egress policy editor ---------------------------------------
// The project's own always-allow / always-block lists, on top of its
// profile's. Decision order (backend/egress.py decide): cut, network off,
// project block, profile block, project allow, profile allow, live auto
// allow, the profile's new-sites rule.
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
  if (pol.fixed || slug === GENERAL) {
    return (
      <div className="sbx-card">
        <div className="sbx-sec-head"><h3>Project lists</h3></div>
        <div className="small">{pol.fixed
          ? <>The image builders' fixed, registry-only policy — it cannot be edited.</>
          : <>This is the Default profile's list: edit it on <Link to="/security/profiles">Profiles</Link>.</>}</div>
        {pol.effective_allow.length > 0 && (
          <div className="dim small">always allows: {pol.effective_allow.join(', ')}</div>)}
      </div>
    )
  }
  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>Project lists</h3>
        <span className="dim small">{status}</span></div>
      <div className="net-policy">
        <div className="small">
          <b>{pol.profile?.name || 'Default'}</b> profile
          <span className="dim"> · new sites: {newSitesText({
            network_off: pol.profile?.network_off, default_verdict: pol.profile?.default })}</span>
          {' '}<Link to="/security/profiles" className="small">edit profile</Link>
        </div>
        <div className="bx-two">
          <Input textarea label="Always allow — this project (one site per line)" className="md-editor" rows={4}
                 spellCheck={false} value={allowText} onChange={(e) => setAllowText(e.target.value)} />
          <Input textarea label="Always block — this project (wins)" className="md-editor" rows={4}
                 spellCheck={false} value={denyText} onChange={(e) => setDenyText(e.target.value)} />
        </div>
        <div className="row">
          <span className="grow" />
          <Button disabled={saving} onClick={save}>{saving ? 'Saving…' : 'Save lists'}</Button>
        </div>
        {pol.effective_allow.length > 0 && (
          <div className="dim small">with the profile, always allowed: {pol.effective_allow.join(', ')}</div>)}
        {pol.effective_deny.length > 0 && (
          <div className="dim small">with the profile, always blocked: {pol.effective_deny.join(', ')}</div>)}
      </div>
    </div>
  )
}

// ---- per-project LAN access -------------------------------------------------
// Box -> LAN only, through the host proxy: the guest still has no route to the
// LAN. Entries are RFC1918 CIDRs, addresses or names, optionally :port. The
// Jav3 host, loopback, link-local/metadata and the box network are refused at
// save and again at connect, even inside a listed range.
function LanAccess({ slug }) {
  const [lan, setLan] = useState(null)
  const [text, setText] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  function load() {
    getLan(slug).then((l) => { setLan(l); setText(l.allow.join('\n')) })
      .catch(() => setLan(null))
  }
  useEffect(() => { load() }, [slug]) // eslint-disable-line

  async function send(change) {
    setBusy(true)
    try {
      await putLan(slug, change)
      setStatus('saved'); setTimeout(() => setStatus(''), 1500); load()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  if (!lan || slug === GENERAL || slug === IMAGE_BUILD) return null
  return (
    <div className="sbx-card">
      <div className="sbx-sec-head"><h3>LAN access</h3>
        <span className="dim small">{status}</span></div>
      <div className="net-policy">
        <Toggle checked={lan.enabled} disabled={busy} label="LAN access"
                onText="On: this project's boxes may reach the devices listed below"
                offText="Off"
                onChange={(on) => send({ enabled: on, allow: parseHosts(text) })} />
        {!lan.enabled && (
          <div className="dim small">Off: boxes in this project cannot reach any
            private or LAN address (NAS, Home Assistant, router).</div>)}
        <Input textarea label="Allowed LAN targets (one per line: 10.0.0.0/24, 10.0.0.60:8123, nas.lan)"
               className="md-editor" rows={4} spellCheck={false} value={text}
               onChange={(e) => setText(e.target.value)} />
        <div className="row">
          <span className="dim small grow">
            Never reachable, even inside a listed range: this server
            {lan.hostIps.length > 0 && <> ({lan.hostIps.join(', ')})</>}, loopback,
            link-local / cloud metadata and the box network.
          </span>
          <Button disabled={busy} onClick={() => send({ allow: parseHosts(text) })}>
            {busy ? 'Saving…' : 'Save LAN list'}</Button>
        </div>
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
      <LanAccess slug={slug} />
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
          <summary>Lists, LAN access and secret grants for {names[filter] || filter}</summary>
          <PolicyEditor slug={filter} />
          <LanAccess slug={filter} />
          <Grants slug={filter} />
        </details>
      ) : (
        <div className="dim small">pick a project above to edit its own lists,
          LAN access and secret grants</div>
      )}
    </div>
  )
}
