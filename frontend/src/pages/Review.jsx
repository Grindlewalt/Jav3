import { useEffect, useState } from 'react'
import { api, subscribeSse } from '../api.js'
import DiffView from '../DiffView.jsx'

// One cross-project queue of everything awaiting the operator: staged diffs +
// gate flags, git commit requests, egress host approvals, and security alerts.
// Rendered whole on the global /review page and, with a `slug`, filtered to a
// single project inside a Workspace panel.
//
// EVERY string in here — staged paths, flag triggers/details, commit messages,
// egress hosts, alert summaries/details, diff bodies — is UNTRUSTED (it comes
// from the agent, from the guest, or from scanned/egress data). All of it is
// rendered as plain text nodes; nothing here goes through <Md>.

const SEV = { info: 'info', warn: 'warn', warning: 'warn', critical: 'crit', crit: 'crit' }
const sevClass = (s) => SEV[String(s || 'info').toLowerCase()] || 'info'

function ts(s) { return s ? String(s).replace('T', ' ').slice(0, 16) : '' }

// ---- a single staged file: status, gate flags, on-demand diff, actions -------
function StagedFile({ slug, entry, flags, blocked, busy, onAck, onAct }) {
  const [open, setOpen] = useState(false)
  const [diff, setDiff] = useState(null)
  const isBlocked = blocked.includes(entry.path)
  const myFlags = flags.filter((f) => f.path === entry.path)

  async function toggle() {
    if (open) { setOpen(false); return }
    setOpen(true)
    if (!diff) {
      try { setDiff(await api(`/api/projects/${slug}/staging/diff?path=${encodeURIComponent(entry.path)}`)) }
      catch { setDiff(null) }
    }
  }

  return (
    <li className="rev-staged">
      <div className="rev-staged-row">
        <span className={`tag ${entry.status}`}>{entry.status}</span>
        <span className="grow ellipsis" title={entry.path}>{entry.path}</span>
        <button className="ghost" onClick={toggle}>{open ? 'hide' : 'diff'}</button>
        <button className="win-btn ok" title={isBlocked
                  ? 'blocked — acknowledge its flags first' : 'approve'}
                disabled={busy || isBlocked}
                onClick={() => onAct('approve', [entry.path])}>✓</button>
        <button className="win-btn" title="reject" disabled={busy}
                onClick={() => onAct('reject', [entry.path])}>✕</button>
      </div>
      {myFlags.map((f) => (
        <div key={f.id} className="rev-flag">
          <span className="grow">
            <span className="mono">{f.trigger}</span>
            {f.detail && <span className="dim"> — {f.detail}</span>}
          </span>
          {f.acknowledged
            ? <span className="tag done">acked</span>
            : <button className="ghost" disabled={busy}
                      onClick={() => onAck(f.id)}>acknowledge</button>}
        </div>
      ))}
      {open && diff && (
        <div className="rev-diff"><DiffView oldText={diff.old} newText={diff.new} /></div>
      )}
    </li>
  )
}

export function ReviewQueue({ slug }) {
  const [slugs, setSlugs] = useState(slug ? [slug] : null)  // project slugs to cover
  const [names, setNames] = useState({})                     // slug -> display name
  const [staging, setStaging] = useState({})                 // slug -> {staged,flags,blocked}
  const [gitReqs, setGitReqs] = useState({})                 // slug -> [pending requests]
  const [pending, setPending] = useState([])                 // egress host approvals
  const [alerts, setAlerts] = useState([])                   // unacknowledged security events
  const [busy, setBusy] = useState(false)

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
    api(`/api/projects/${s}/staging`).then((r) =>
      setStaging((m) => ({ ...m, [s]: {
        staged: r.staged || [], flags: r.flags || [], blocked: r.blocked || [] } }))).catch(() => {})
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

  async function ackFlag(s, id) {
    try { await api(`/api/projects/${s}/staging/flags/${id}/ack`, { method: 'POST' }); loadProject(s) }
    catch (e) { window.alert(e.detail || String(e)) }
  }
  async function stageAct(s, verb, paths) {
    setBusy(true)
    try {
      await api(`/api/projects/${s}/staging/${verb}`, {
        method: 'POST', body: JSON.stringify({ paths }) })
      loadProject(s)
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (e) { window.alert(e.detail || String(e)) }
    setBusy(false)
  }
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

  const multi = !slug && (slugs?.length || 0) > 1
  const projLabel = (s) => names[s] || s
  const stagedTotal = (slugs || []).reduce((n, s) => n + (staging[s]?.staged?.length || 0), 0)
  const gitTotal = (slugs || []).reduce((n, s) => n + (gitReqs[s]?.length || 0), 0)
  const total = alerts.length + stagedTotal + gitTotal + pending.length

  if (!slugs) return <div className="dim center-pad">…</div>

  return (
    <div className="review-queue">
      {total === 0 && (
        <div className="dim center-pad">nothing waiting on you — all clear ✓</div>
      )}

      {/* ---- security alerts (most urgent first) ---- */}
      {alerts.length > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Security alerts</h3>
            <span className="dim small">{alerts.length} unacknowledged</span>
          </div>
          {alerts.map((a) => <AlertRow key={a.id} a={a} onAck={ackAlert} />)}
        </section>
      )}

      {/* ---- staged changes + gate flags ---- */}
      {stagedTotal > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Staged changes</h3>
            <span className="dim small">{stagedTotal} file{stagedTotal !== 1 && 's'} · a flagged file
              can't be approved until its flags are acknowledged</span>
          </div>
          {(slugs || []).map((s) => {
            const st = staging[s]
            if (!st || st.staged.length === 0) return null
            return (
              <div key={s} className="rev-group">
                {multi && <div className="rev-group-head">📁 {projLabel(s)}</div>}
                <ul className="staged-list rev-list">
                  {st.staged.map((e) => (
                    <StagedFile key={e.path} slug={s} entry={e}
                                flags={st.flags} blocked={st.blocked} busy={busy}
                                onAck={(id) => ackFlag(s, id)}
                                onAct={(verb, paths) => stageAct(s, verb, paths)} />
                  ))}
                </ul>
              </div>
            )
          })}
        </section>
      )}

      {/* ---- git commit requests ---- */}
      {gitTotal > 0 && (
        <section className="sbx-sec">
          <div className="sbx-sec-head">
            <h3>Commit requests</h3>
            <span className="dim small">approving commits (and pushes, when a remote is set)</span>
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
            <h3>Egress host approvals</h3>
            <span className="dim small">hosts the guest tried to reach — approve to train the allowlist</span>
          </div>
          <ul className="staged-list rev-list">
            {pending.map((p) => (
              <li key={p.id}>
                <span className="tag pending">{p.hit_count}×</span>
                <span className="grow ellipsis" title={p.host}>{p.host}</span>
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
    </div>
  )
}

function AlertRow({ a, onAck }) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`sbx-row sev-${sevClass(a.severity)}`}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="sbx-verdict-top" style={{ marginBottom: 2 }}>
          <span className={`tag sev-${sevClass(a.severity)}-tag`}>{a.severity}</span>
          <span className="mono small">{a.kind}</span>
          {a.project_slug && <span className="tag">{a.project_slug}</span>}
          <span className="dim small">{ts(a.created_at)}</span>
        </div>
        <div className="rev-alert-summary">{a.summary}</div>
        {open && a.detail && <pre className="log-pre rev-alert-detail">{a.detail}</pre>}
      </div>
      <div className="sbx-right">
        {a.detail && <button className="ghost" onClick={() => setOpen((o) => !o)}>
          {open ? 'less' : 'detail'}</button>}
        <button className="ghost" onClick={() => onAck(a.id)}>Acknowledge</button>
      </div>
    </div>
  )
}

export default function Review() {
  return (
    <div className="page review-page">
      <h2>Review Center</h2>
      <p className="dim small">Everything waiting on you, across every project — staged edits,
        commit requests, egress approvals and security alerts, with inline actions.</p>
      <ReviewQueue />
    </div>
  )
}
