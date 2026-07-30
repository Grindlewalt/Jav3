import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { api, subscribeSse } from '../api.js'
import SecurityBoard from '../SecurityBoard.jsx'
import TriagePanel from '../TriagePanel.jsx'

// One cross-project queue of everything awaiting the operator: git commit
// requests, egress host approvals, and security alerts (which now include the
// advisory write flags — file writes apply live, the diff gate alerts instead
// of blocking). Rendered whole on the global /review page and, with a `slug`,
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

const SEV = { info: 'info', warn: 'warn', warning: 'warn', critical: 'crit', crit: 'crit' }
const sevClass = (s) => SEV[String(s || 'info').toLowerCase()] || 'info'

function ts(s) { return s ? String(s).replace('T', ' ').slice(0, 16) : '' }

export function ReviewQueue({ slug }) {
  const [slugs, setSlugs] = useState(slug ? [slug] : null)  // project slugs to cover
  const [names, setNames] = useState({})                     // slug -> display name
  const [gitReqs, setGitReqs] = useState({})                 // slug -> [pending requests]
  const [pending, setPending] = useState([])                 // egress host approvals
  const [alerts, setAlerts] = useState([])                   // unacknowledged security events
  const [busy, setBusy] = useState(false)
  const [board, setBoard] = useState(null)   // {id, seed} — the open evidence board
  const loc = useLocation()

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

  const key = slugs ? slugs.join(',') : ''
  useEffect(() => {
    if (!slugs) return
    const refresh = () => { slugs.forEach(loadProject); loadEgress(); loadAlerts() }
    refresh()
    const t = setInterval(refresh, 12000)
    const h = () => refresh()
    window.addEventListener('jarvis-files-changed', h)
    return () => { clearInterval(t); window.removeEventListener('jarvis-files-changed', h) }
  }, [key]) // eslint-disable-line

  // live security alerts prepend as they fire
  useEffect(() => {
    return subscribeSse('/api/security/stream', (ev) => {
      if (ev.type !== 'security_event') return
      const proj = ev.project_slug || ev.project
      if (slug && proj !== slug) return
      setAlerts((a) => a.some((x) => x.id === ev.id) ? a : [{
        id: ev.id, kind: ev.kind, severity: ev.severity, project_slug: proj,
        summary: ev.summary, detail: ev.detail, acknowledged: false,
        created_at: ev.created_at }, ...a])
    })
  }, [slug])

  async function gitAct(s, id, verb) {
    if (verb === 'reject' && !window.confirm(`reject commit request #${id}?`)) return
    setBusy(true)
    try {
      await api(`/api/projects/${s}/git/requests/${id}/${verb}`, { method: 'POST' })
      loadProject(s)
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { window.alert(e.detail || String(e)) }
    setBusy(false)
  }
  async function egressAct(id, verb) {
    try { await api(`/api/egress/pending/${id}/${verb}`, { method: 'POST' }); loadEgress() }
    catch (e) { window.alert(e.detail || String(e)) }
  }
  async function ackAlert(id) {
    try { await api(`/api/security/events/${id}/ack`, { method: 'POST' })
      setAlerts((a) => a.filter((x) => x.id !== id)) }
    catch (e) { window.alert(e.detail || String(e)) }
  }

  // bulk verdicts — the queues reached hundreds; one server call each.
  // approve trains the allowlist for every host, so it confirms hardest.
  const BULK_ASK = {
    approve: (n) => `Approve all ${n} hosts? Every one is added to the allowlist `
      + '— including the ⚑ flagged ones.',
    reject: (n) => `Reject all ${n} hosts? They stay blocked and re-queue if hit again.`,
    dismiss: (n) => `Dismiss all ${n} hosts? No verdict — the queue just clears; `
      + 'a host that is hit again comes back.',
  }
  async function egressBulk(action) {
    if (!window.confirm(BULK_ASK[action](pending.length))) return
    setBusy(true)
    try {
      await api('/api/egress/pending/bulk', {
        method: 'POST',
        body: JSON.stringify({ action, project: slug || null }) })
      loadEgress()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { window.alert(e.detail || String(e)) }
    setBusy(false)
  }
  async function ackAllAlerts() {
    if (!window.confirm(`Acknowledge all ${alerts.length} alerts?`)) return
    setBusy(true)
    try {
      await api('/api/security/events/ack_all', { method: 'POST' })
      loadAlerts()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { window.alert(e.detail || String(e)) }
    setBusy(false)
  }

  const multi = !slug && (slugs?.length || 0) > 1
  const projLabel = (s) => names[s] || s
  const gitTotal = (slugs || []).reduce((n, s) => n + (gitReqs[s]?.length || 0), 0)
  const total = alerts.length + gitTotal + pending.length

  if (!slugs) return <div className="dim center-pad">…</div>

  return (
    <div className="review-queue">
      {total === 0 && (
        <div className="dim center-pad">nothing waiting on you — all clear ✓</div>
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
                      <span className="grow ellipsis" title={r.message}>{r.message}</span>
                      {r.error && <span className="tag error" title={r.error}>retry</span>}
                      <button className="win-btn ok" title="approve: commit + push"
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

      {/* ---- egress host approvals ---- */}
      {pending.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Egress hosts</h3>
            <span className="sec-count">{pending.length}</span>
            <div className="sec-actions">
              <button className="ghost" disabled={busy}
                      title="add every host to the allowlist"
                      onClick={() => egressBulk('approve')}>✓ Approve all</button>
              <button className="ghost danger" disabled={busy}
                      title="keep every host blocked"
                      onClick={() => egressBulk('reject')}>✕ Reject all</button>
              <button className="ghost" disabled={busy}
                      title="clear the queue without a verdict"
                      onClick={() => egressBulk('dismiss')}>Dismiss all</button>
            </div>
          </div>
          <ul className="staged-list rev-list">
            {pending.map((p) => (
              <li key={p.id}>
                <span className="tag pending">{p.hit_count}×</span>
                <span className="grow ellipsis" title={p.host}>{p.host}</span>
                {p.triage_verdict === 'flag' && (
                  <span className="tag triage-flag" title={p.triage_reason}>⚑ {p.triage_reason}</span>)}
                {!slug && p.project_slug && <span className="tag">{p.project_slug}</span>}
                <button className="win-btn ok" title="approve host"
                        onClick={() => egressAct(p.id, 'approve')}>✓</button>
                <button className="win-btn" title="reject host"
                        onClick={() => egressAct(p.id, 'reject')}>✕</button>
              </li>
            ))}
          </ul>
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
  const subject = subjectOf(a.detail)
  return (
    <div className={`sbx-row sev-${sev}`}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="sbx-verdict-top" style={{ marginBottom: 2 }}>
          <span className={`tag sev-${sev}-tag`}>{a.severity}</span>
          <span className="mono small">{a.kind}</span>
          {a.project_slug && <span className="tag">{a.project_slug}</span>}
          {a.triage_verdict === 'flag' && (
            <span className="tag triage-flag" title={a.triage_reason}>⚑ {a.triage_reason}</span>)}
          <span className="dim small">{ts(a.created_at)}</span>
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

export default function Review() {
  return (
    <div className="page review-page">
      <h2>Review Center</h2>
      <TriagePanel />
      <ReviewQueue />
    </div>
  )
}
