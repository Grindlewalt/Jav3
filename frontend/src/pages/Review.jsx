import { useContext, useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { api, subscribeSse } from '../api.js'
import SecurityBoard from '../SecurityBoard.jsx'
import TriagePanel from '../TriagePanel.jsx'
import { useEgressDecide } from '../EgressDecide.jsx'
import { PendingCountContext } from '../Notices.jsx'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { sevClass, ts } from '../format.js'
import {
  ALLOW_ALWAYS_TIP, ALLOW_ONCE_TIP, DENY_TIP, REFUSED_TAG, ledeFor, SECURITY_LEDES,
} from '../securityCopy.js'
import EmptyState from '../components/EmptyState.jsx'
import Page from '../components/Page.jsx'
import Tabs from '../components/Tabs.jsx'
import Button from '../components/Button.jsx'
import { SERVICES_POLL_MS, followServices, listServices } from '../boxes/api/services.js'
import { listPackages } from '../boxes/api/packages.js'
import { listProfiles } from '../boxes/api/profiles.js'
import { listImages } from '../boxes/api/images.js'
import { needsProject } from '../boxes/logic.js'
import {
  PackageApprove, PackageSummary, ServiceRequest, usePackageReject,
} from '../boxes/RequestCards.jsx'

// One cross-project queue of everything awaiting the operator: git commit
// requests, egress host approvals, and security alerts (which now include the
// advisory write flags — file writes apply live, the diff gate alerts instead
// of blocking). Rendered whole on the global /security page and, with a `slug`,
// filtered to a single project inside a Workspace panel.
//
// EVERY string in here — flag triggers/details, commit messages, egress hosts,
// alert summaries/details — is UNTRUSTED (it comes from the agent, from the
// guest, or from scanned/egress data). All of it is rendered as plain text
// nodes; nothing here goes through <Md>.
//
// An alert row is a headline, not the evidence: "Inspect" opens the
// SecurityBoard, which is where the flagged code, the diff, the directory and
// the traffic live. A security toast deep-links straight into it via router
// state (`openEvent`), so a card that drains away is still recoverable.

export function ReviewQueue({ slug }) {
  const [slugs, setSlugs] = useState(slug ? [slug] : null)  // project slugs to cover
  const [names, setNames] = useState({})                     // slug -> display name
  const [gitReqs, setGitReqs] = useState({})                 // slug -> [pending requests]
  const [pending, setPending] = useState([])                 // egress host approvals
  const [alerts, setAlerts] = useState([])                   // unacknowledged security events
  const [busy, setBusy] = useState(false)
  const [board, setBoard] = useState(null)   // {id, seed} — the open evidence board
  // the boxes requests (WP3 services, WP5 packages). Either route may not
  // exist on this server yet: then the section simply never shows.
  const [svcReqs, setSvcReqs] = useState([])
  const [lan, setLan] = useState(null)       // {ip, configured, error}
  const [pkgReqs, setPkgReqs] = useState([])
  const [profiles, setProfiles] = useState([])
  const [variants, setVariants] = useState(null)
  const [approvingPkg, setApprovingPkg] = useState(null)
  const loc = useLocation()
  const ask = useAsk()

  // arriving from a security toast: open that event's board straight away.
  // Keyed on loc.key as well as the id so clicking a second toast for the SAME
  // alert re-opens it instead of looking dead.
  useEffect(() => {
    const id = loc.state?.openEvent
    if (id) setBoard({ id, seed: null })
  }, [loc.key]) // eslint-disable-line

  // which projects to cover: the one slug, or all of them
  useEffect(() => {
    if (slug) { setSlugs([slug]); return }
    api('/api/projects').then((r) => {
      const ps = r.projects || []
      setSlugs(ps.map((p) => p.slug))
      const nm = {}; ps.forEach((p) => { nm[p.slug] = p.name })
      setNames(nm)
    }).catch(() => setSlugs([]))
  }, [slug])

  function loadProject(s) {
    api(`/api/projects/${s}/git/requests`).then((r) =>
      setGitReqs((m) => ({ ...m, [s]: (r.requests || []).filter((q) => q.status === 'pending') })))
      .catch(() => {})
  }
  function loadEgress() {
    api(`/api/egress/pending${slug ? `?project=${encodeURIComponent(slug)}` : ''}`)
      .then((r) => setPending(r.pending || [])).catch(() => {})
  }
  function loadAlerts() {
    api('/api/security/events?unacknowledged=true').then((r) => {
      let evs = r.events || []
      if (slug) evs = evs.filter((e) => (e.project_slug || e.project) === slug)
      setAlerts(evs)
    }).catch(() => {})
  }

  function loadSvcReqs() {
    listServices(slug || undefined).then((r) => {
      setSvcReqs(r.services.filter((x) => x.status === 'pending'))
      setLan({ ip: r.lanIp, configured: r.lanConfigured, error: r.lanError })
    }).catch(() => setSvcReqs([]))
  }
  function loadBoxReqs() {
    loadSvcReqs()
    loadPkgReqs()
  }
  function loadPkgReqs() {
    listPackages('pending').then((rows) =>
      setPkgReqs(slug ? rows.filter((r) => r.project_slug === slug) : rows))
      .catch(() => setPkgReqs([]))
  }
  useEffect(() => {
    listProfiles().then(setProfiles).catch(() => {})
    listImages().then((r) => setVariants(r.variants)).catch(() => {})
  }, [])
  const rejectPkg = usePackageReject(loadBoxReqs)
  const placementOf = (proj) => {
    const p = profiles.find((x) => (x.projects || []).includes(proj))
      || profiles.find((x) => x.is_default)
    return p?.service_placement || ''
  }

  const key = slugs ? slugs.join(',') : ''
  useEffect(() => {
    if (!slugs) return
    // service requests ride topic `services` (below) with a slow fallback
    // poll; everything else keeps the 12 s refresh
    const refresh = () => {
      slugs.forEach(loadProject); loadEgress(); loadAlerts(); loadPkgReqs()
    }
    refresh()
    loadSvcReqs()
    const t = setInterval(refresh, 12000)
    const tSvc = setInterval(loadSvcReqs, SERVICES_POLL_MS)
    const stopSvc = followServices(() => loadSvcReqs())
    const h = () => { refresh(); loadSvcReqs() }
    window.addEventListener('jarvis-files-changed', h)
    return () => {
      clearInterval(t); clearInterval(tSvc); stopSvc()
      window.removeEventListener('jarvis-files-changed', h)
    }
  }, [key]) // eslint-disable-line

  // live security alerts prepend as they fire; a repeat (the server coalesced
  // it onto a row still in the queue) only bumps that row's count
  useEffect(() => {
    return subscribeSse('/api/security/stream', (ev) => {
      if (ev.type !== 'security_event') return
      const proj = ev.project_slug || ev.project
      if (slug && proj !== slug) return
      setAlerts((a) => a.some((x) => x.id === ev.id)
        ? a.map((x) => (x.id === ev.id && ev.count
          ? { ...x, count: ev.count, last_seen: new Date().toISOString() } : x))
        : [{
          id: ev.id, kind: ev.kind, severity: ev.severity, project_slug: proj,
          summary: ev.summary, detail: ev.detail, acknowledged: false,
          created_at: ev.created_at, count: ev.count || 1, tier: ev.tier }, ...a])
    })
  }, [slug])

  async function gitAct(s, id, verb) {
    if (verb === 'reject'
        && !await ask.confirm(`Reject commit request #${id}?`,
                              { confirmLabel: 'Reject', danger: true })) return
    setBusy(true)
    try {
      await api(`/api/projects/${s}/git/requests/${id}/${verb}`, { method: 'POST' })
      loadProject(s)
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { notifyError(e) }
    setBusy(false)
  }
  // Allow always / Allow 1 h / Deny, the same three words and the same project
  // picker as the Network tab (an unattributed row is put on a project you choose)
  const { decide: egressAct, picker } = useEgressDecide(loadEgress, { project: slug || null, names })
  async function ackAlert(id) {
    try { await api(`/api/security/events/${id}/ack`, { method: 'POST' })
      setAlerts((a) => a.filter((x) => x.id !== id)) }
    catch (e) { notifyError(e) }
  }

  // bulk verdicts — the queues reached hundreds; one server call each.
  // Allow all trains the allowlist for every host, so it confirms hardest.
  const BULK_ASK = {
    approve: (n) => `Allow all ${n} sites? Each goes on its project's always-allow list, `
      + 'including the ⚑ flagged ones. Addresses that can never be allowed, like the Jav3 '
      + 'host, are skipped.',
    reject: (n) => `Deny all ${n} sites? They stay blocked and come back if a box asks again.`,
    dismiss: (n) => `Clear all ${n} sites from this list? No decision is recorded; `
      + 'a site a box asks for again comes back.',
  }
  const BULK_LABEL = { approve: 'Allow all', reject: 'Deny all', dismiss: 'Clear list' }
  async function egressBulk(action) {
    if (!await ask.confirm(BULK_ASK[action](pending.length),
                           { confirmLabel: BULK_LABEL[action],
                             danger: action !== 'dismiss' })) return
    setBusy(true)
    try {
      const r = await api('/api/egress/pending/bulk', {
        method: 'POST',
        body: JSON.stringify({ action, project: slug || null }) })
      if (r?.skipped) notify(`${r.skipped} skipped: they cannot be allowed`, { life: 8 })
      loadEgress()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { notifyError(e) }
    setBusy(false)
  }
  async function ackAllAlerts() {
    if (!await ask.confirm(`Acknowledge all ${alerts.length} alerts?`,
                           { confirmLabel: 'Acknowledge all' })) return
    setBusy(true)
    try {
      await api('/api/security/events/ack_all', { method: 'POST' })
      loadAlerts()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { notifyError(e) }
    setBusy(false)
  }

  const multi = !slug && (slugs?.length || 0) > 1
  const projLabel = (s) => names[s] || s
  const gitTotal = (slugs || []).reduce((n, s) => n + (gitReqs[s]?.length || 0), 0)
  const total = alerts.length + gitTotal + pending.length + svcReqs.length + pkgReqs.length

  if (!slugs) return <div className="dim center-pad">…</div>

  return (
    <div className="review-queue">
      {total === 0 && (
        <EmptyState pad>nothing waiting on you — all clear ✓</EmptyState>
      )}

      {/* ---- git commit requests (deliberately no bulk verdict: each one is
             a push to a repo, they deserve individual eyes) ---- */}
      {gitTotal > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Commit requests</h3>
            <span className="sec-count">{gitTotal}</span>
          </div>
          {(slugs || []).map((s) => {
            const reqs = gitReqs[s] || []
            if (reqs.length === 0) return null
            return (
              <div key={s} className="rev-group">
                {multi && <div className="rev-group-head">📁 {projLabel(s)}</div>}
                <ul className="staged-list rev-list">
                  {reqs.map((r) => (
                    <li key={r.id}>
                      <span className="tag new">#{r.id}</span>
                      {r.kind === 'push' && <span className="tag">PR</span>}
                      <span className="grow ellipsis"
                            title={r.kind === 'push' && r.summary
                              ? `${r.message}\n\n${r.branch}\n${r.summary}` : r.message}>
                        {String(r.message || '').split('\n')[0]}
                        {r.kind === 'push' && r.summary && (
                          <span className="dim small"> · {r.summary.split('\n').pop()}</span>)}
                      </span>
                      {r.kind === 'push' && r.pr_url && (
                        <a className="win-btn" href={r.pr_url} target="_blank" rel="noreferrer"
                           title={`review the diff in Gitea (${r.branch})`}>diff ↗</a>)}
                      {r.error && <span className="tag error" title={r.error}>retry</span>}
                      <button className="win-btn ok"
                              title={r.kind === 'push' ? 'approve: merge the pull request into main'
                                : 'approve: commit + push'}
                              disabled={busy} onClick={() => gitAct(s, r.id, 'approve')}>✓</button>
                      <button className="win-btn" title="reject" disabled={busy}
                              onClick={() => gitAct(s, r.id, 'reject')}>✕</button>
                    </li>
                  ))}
                </ul>
              </div>
            )
          })}
        </section>
      )}

      {/* ---- service requests: each one individually, never in bulk, and
             never auto-handled (the reviewer's never-list) ---- */}
      {svcReqs.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Service requests</h3>
            <span className="sec-count">{svcReqs.length}</span>
          </div>
          {svcReqs.map((x) => (
            <ServiceRequest key={x.id} s={x} lan={lan}
                            profilePlacement={placementOf(x.project_slug)}
                            onDone={loadBoxReqs} />
          ))}
        </section>
      )}

      {/* ---- package requests ---- */}
      {pkgReqs.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Package requests</h3>
            <span className="sec-count">{pkgReqs.length}</span>
          </div>
          {pkgReqs.map((p) => (
            <div key={p.id} className="sbx-row sev-warn bx-cat-row">
              <div className="grow"><PackageSummary p={p} />
                {p.card && <div className="small bx-reach">{p.card}</div>}</div>
              <div className="sbx-right bx-cat-right">
                <span className="small">into <code>{p.target_variant}</code></span>
                <span className="row">
                  <Button variant="ghost" onClick={() => setApprovingPkg(p)}>Approve…</Button>
                  <Button variant="ghost" danger onClick={() => rejectPkg(p)}>Reject</Button>
                </span>
              </div>
            </div>
          ))}
          <PackageApprove p={approvingPkg} variants={variants}
                          onClose={() => setApprovingPkg(null)}
                          onDone={() => { setApprovingPkg(null); loadBoxReqs() }} />
        </section>
      )}

      {/* ---- egress host approvals ---- */}
      {pending.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Sites boxes asked for</h3>
            <span className="sec-count">{pending.length}</span>
            <div className="sec-actions">
              <button className="ghost" disabled={busy}
                      title="put every site on its project's always-allow list"
                      onClick={() => egressBulk('approve')}>Allow all</button>
              <button className="ghost danger" disabled={busy}
                      title="keep every site blocked"
                      onClick={() => egressBulk('reject')}>Deny all</button>
              <button className="ghost" disabled={busy}
                      title="empty the list without deciding anything"
                      onClick={() => egressBulk('dismiss')}>Clear list</button>
            </div>
          </div>
          <p className="dim small net-lede">A box tried to reach these and no list covers them,
            so they were blocked. "Allow always" adds the site to that project's always-allow
            list for good; "Allow 1 h" lets it through for an hour and writes no list.</p>
          <ul className="staged-list rev-list">
            {pending.map((p) => (
              <li key={p.id} className="rev-egress">
                <span className="tag pending">{p.hit_count}×</span>
                <span className="grow ellipsis" title={p.host}>{p.host}</span>
                {p.refused && <span className="tag error" title={p.refused}>{REFUSED_TAG}</span>}
                {p.triage_verdict === 'flag' && (
                  <span className="tag triage-flag" title={p.triage_reason}>⚑ {p.triage_reason}</span>)}
                {!slug && p.project_slug && !needsProject(p) && <span className="tag">{p.project_slug}</span>}
                {needsProject(p) && <span className="tag pending" title="you pick the project when you allow it">no project</span>}
                {p.refused && <span className="dim small net-refused">{p.refused}</span>}
                <span className="rev-egress-btns">
                  {!p.refused && <>
                    <Button variant="ghost" title={ALLOW_ALWAYS_TIP(needsProject(p) ? '' : projLabel(p.project_slug))}
                            onClick={() => egressAct(p, 'allow')}>Allow always</Button>
                    <Button variant="ghost" title={ALLOW_ONCE_TIP}
                            onClick={() => egressAct(p, 'once')}>Allow 1 h</Button></>}
                  <Button variant="ghost" title={DENY_TIP} onClick={() => egressAct(p, 'deny')}>Deny</Button>
                </span>
              </li>
            ))}
          </ul>
          {picker}
        </section>
      )}

      {/* ---- security alerts ---- */}
      {alerts.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Security alerts</h3>
            <span className="sec-count">{alerts.length}</span>
            {/* ack_all is global — inside a single project's Workspace panel it
                would silently clear other projects' alerts, so it stays off */}
            {!slug && (
              <div className="sec-actions">
                <button className="ghost" disabled={busy}
                        title="mark every alert as seen"
                        onClick={ackAllAlerts}>Acknowledge all</button>
              </div>
            )}
          </div>
          {alerts.map((a) => (
            <AlertRow key={a.id} a={a} onAck={ackAlert}
                      onOpen={() => setBoard({ id: a.id, seed: a })} />
          ))}
        </section>
      )}

      {board && (
        <SecurityBoard eventId={board.id} seed={board.seed}
                       onClose={() => setBoard(null)} onAck={ackAlert} />
      )}
    </div>
  )
}

// The one thing worth seeing without opening the board: WHAT the alert is
// about. A queue of "write flag: new_import" rows is unscannable; a queue of
// paths and hostnames is.
function subjectOf(d) {
  if (!d || typeof d !== 'object') return null
  return d.path || d.host || d.username || d.peer || null
}

function AlertRow({ a, onAck, onOpen }) {
  const sev = sevClass(a.severity)
  // a summary that already names its subject ("… (from 10.0.0.82)") does not
  // need the subject again on the line under it
  const subj = subjectOf(a.detail)
  const subject = subj && !String(a.summary || '').includes(subj) ? subj : null
  return (
    <div className={`sbx-row sev-${sev}`}>
      <div className="grow rev-alert-main">
        <div className="sbx-verdict-top rev-alert-top">
          <span className={`tag sev-${sev}-tag`}>{a.severity}</span>
          <span className="mono small">{a.kind}</span>
          {a.project_slug && <span className="tag">{a.project_slug}</span>}
          {/* the same alert again while this row waited: counted, not re-listed */}
          {a.count > 1 && (
            <span className="tag" title={`first ${ts(a.created_at)}, last ${ts(a.last_seen)} UTC`}>
              ×{a.count}</span>)}
          {a.triage_verdict === 'flag' && (
            <span className="tag triage-flag" title={a.triage_reason}>⚑ {a.triage_reason}</span>)}
          <span className="dim small">{ts(a.count > 1 && a.last_seen ? a.last_seen : a.created_at)}</span>
        </div>
        {/* the whole summary is the affordance — clicking it opens the board */}
        <button type="button" className="rev-alert-open" onClick={onOpen}
                title="open the evidence board">
          <span className="rev-alert-summary">{a.summary}</span>
          {subject && <span className="mono small rev-alert-subject">{subject}</span>}
        </button>
      </div>
      <div className="sbx-right">
        <button className="ghost" onClick={onOpen}
                title="the flagged code, the diff, the directory, the traffic">
          Inspect</button>
        <button className="ghost" onClick={() => onAck(a.id)}>Acknowledge</button>
      </div>
    </div>
  )
}

// ---- the shell ----
// Security (the page once called Review) is a layout route (routes.jsx): this
// is the <h1> and the tab strip, and the tabs are real URLs — /security,
// /security/network, /security/logs, /security/secrets — so each is linkable
// and NavLink lights the current one. The old /review* addresses redirect.
// Network and Logs used to be top-level pages; they are the guest's traffic
// and the agent's transcripts, which is to say evidence, and evidence belongs
// beside the queue that cites it. The old /network and /logs redirect here.
//
// THE CONTRACT for a tab (an <Outlet> child): it renders inside .review-body,
// a full-height flex column that owns the insets and scrolls if the child
// does not. A tab that wants its own inner scrolling (Network's feed, Logs'
// transcript) is `flex: 1; min-height: 0` and never overflows the body; a
// tab that is a plain document (the queue, Secrets) just flows and the body
// scrolls. No tab paints a heading of its own.
export default function Review() {
  const count = useContext(PendingCountContext)
  const { pathname } = useLocation()
  return (
    <Page variant="fill" title="Security" className="review-shell"
          actions={(
            <Tabs label="Security sections" items={[
              { to: '/security', end: true, label: 'Queue', count },
              { to: '/security/persistent', label: 'Persistent' },
              { to: '/security/network', label: 'Network' },
              { to: '/security/profiles', label: 'Profiles' },
              { to: '/security/logs', label: 'Logs' },
              { to: '/security/secrets', label: 'Secrets' },
            ]} />
          )}>
      <div className="review-body">
        {/* one line on what this tab is, in the tab's own column */}
        {ledeFor(SECURITY_LEDES, pathname) && <p className="tab-lede dim">{ledeFor(SECURITY_LEDES, pathname)}</p>}
        <Outlet />
      </div>
    </Page>
  )
}

// The index tab: Auto review's control strip, then the queue itself.
export function ReviewHome() {
  return (
    <div className="review-page">
      <TriagePanel />
      <ReviewQueue />
    </div>
  )
}
