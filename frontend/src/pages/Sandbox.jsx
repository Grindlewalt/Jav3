import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Sandbox review console: queue of sandbox sessions (left), evidence for the
// selected session (center), verdict actions + learned egress rules (right).
// Everything shown here is computed by deterministic backend rules — the
// frontend never judges severity or summarizes; it only displays escaped
// values. Host/path/argv strings are untrusted data: rendered as text in mono.

const VERDICT_LABEL = { crit: 'critical', warn: 'warning', ok: 'clean' }

function relTime(iso) {
  if (!iso) return ''
  let s = String(iso).replace(' ', 'T')
  if (!/[zZ]$|[+-]\d\d:?\d\d$/.test(s)) s += 'Z'
  const t = new Date(s).getTime()
  if (Number.isNaN(t)) return String(iso)
  const mins = Math.max(0, Math.floor((Date.now() - t) / 60000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.floor(mins / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function fmtBytes(n) {
  if (n == null) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function dirArrow(dir) {
  if (dir === 'in' || dir === 'rx' || dir === 'down') return '▼'
  if (dir === 'out' || dir === 'tx' || dir === 'up') return '▲'
  return '▲▼'
}

function Tile({ label, value, sub, bad }) {
  return (
    <div className={`sbx-tile${bad ? ' bad' : ''}`}>
      <div className="n">{value}</div>
      <div className="dim small">{label}{sub ? ` · ${sub}` : ''}</div>
    </div>
  )
}

function Section({ title, note, children }) {
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head">
        <h3>{title}</h3>
        {note && <span className="dim small">{note}</span>}
      </div>
      {children}
    </section>
  )
}

function EgressRow({ row, busy, onDecide }) {
  const label = row.host || row.ip
  const blocked = row.status === 'blocked'
  const attempts = row.attempts || 0
  return (
    <div className={`sbx-row sev-${row.sev || 'ok'}`}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="mono ellipsis">
          {label}<span className="dim">:{row.port}</span>
          {row.host && row.ip && <span className="tag">{row.ip}</span>}
        </div>
        <div className="sbx-sub">
          {row.rule && <span className="sbx-rule-chip">{row.rule}</span>}
          <span className="dim small">
            {blocked
              ? 'not in allowlist'
              : row.learned ? 'learned this session' : 'allowlisted'}
          </span>
        </div>
      </div>
      {blocked ? (
        <div className="sbx-right">
          <span className="sbx-pill crit">blocked</span>
          <span className="dim small">
            blocked · {attempts} attempt{attempts === 1 ? '' : 's'}</span>
          <span className="sbx-actions">
            <button className="ghost" disabled={busy}
                    onClick={() => onDecide(row, 'allow')}>Allow</button>
            <button className="ghost danger" disabled={busy}
                    onClick={() => onDecide(row, 'deny')}>Deny</button>
          </span>
        </div>
      ) : (
        <div className="sbx-right">
          <span className="mono">{dirArrow(row.dir)} {fmtBytes(row.bytes)}</span>
          <span className="dim small">delivered</span>
          <span className="sbx-pill ok">auto-allowed</span>
        </div>
      )}
    </div>
  )
}

function BeaconRow({ b }) {
  return (
    <div className={`sbx-row sev-${b.sev || 'ok'}`}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="ellipsis">
          <span className="dim small">{b.method}</span>{' '}
          <span className="mono">{b.url}</span>
        </div>
        <div className="sbx-sub">
          {b.api && <span className="tag">{b.api}</span>}
          {b.host && <span className="sbx-rule-chip">{b.host}</span>}
          {b.bytes && <span className="dim small">{b.bytes}</span>}
        </div>
      </div>
      <div className="sbx-right">
        {b.external
          ? <span className="sbx-pill crit">external · blocked</span>
          : <span className="sbx-pill ok">local</span>}
      </div>
    </div>
  )
}

export default function Sandbox() {
  const [sessions, setSessions] = useState([])
  const [filter, setFilter] = useState('all')
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [rules, setRules] = useState([])
  const [toasts, setToasts] = useState([])
  const [busy, setBusy] = useState(false)
  const selectedRef = useRef(null)
  // "New sandbox run" control (left rail, above the queue).
  const [projects, setProjects] = useState([])
  const [runProject, setRunProject] = useState('')
  const [runCmd, setRunCmd] = useState('')
  const [running, setRunning] = useState(false)
  const [runErr, setRunErr] = useState('')

  const toast = (msg) => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t, { id, msg }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500)
  }

  const refreshSessions = () =>
    api('/api/sandbox/sessions').then((r) => setSessions(r.sessions)).catch(() => {})
  const refreshRules = () =>
    api('/api/sandbox/rules').then((r) => setRules(r.rules)).catch(() => {})
  const refreshDetail = (id) =>
    api(`/api/sandbox/sessions/${id}`)
      .then((d) => { if (selectedRef.current === id) setDetail(d) })
      .catch(() => {})

  useEffect(() => {
    refreshSessions(); refreshRules()
    api('/api/projects')
      .then((r) => {
        const list = r.projects || []
        setProjects(list)
        if (list.length) setRunProject(r.active || list[0].slug)
      })
      .catch(() => {})
    const t = setInterval(() => {
      refreshSessions(); refreshRules()
      if (selectedRef.current != null) refreshDetail(selectedRef.current)
    }, 5000)
    return () => clearInterval(t)
  }, [])

  function openSession(id) {
    selectedRef.current = id
    setSelected(id); setDetail(null)
    refreshDetail(id)
  }

  // Kick off a monitored VM run from the UI. This is the same gated pipeline
  // that also runs when Jarvis calls the `run_gated` tool in chat, or via the
  // Renderer panel's "Scan for beacons" button — this is just the manual,
  // operator-initiated entry point. Slow: boots a fresh VM + runs (up to ~90s).
  async function startRun() {
    const cmd = runCmd.trim()
    if (!runProject || !cmd) return
    setRunning(true); setRunErr('')
    try {
      const r = await api('/api/vm/gate/run', {
        method: 'POST',
        body: JSON.stringify({ project: runProject, command: cmd, fresh: true }),
      })
      setRunCmd('')
      await refreshSessions()
      if (r && r.run_id != null) openSession(r.run_id)
    } catch (e) {
      setRunErr(e.detail || e.message || 'run failed')
    } finally {
      setRunning(false)
    }
  }

  async function decide(row, decision) {
    const label = row.host || row.ip
    setBusy(true)
    try {
      await api(`/api/sandbox/sessions/${detail.id}/connection`, {
        method: 'POST',
        body: JSON.stringify({ key: row.key, decision }),
      })
      toast(decision === 'allow'
        ? `Allowed ${label} — added to the allowlist; this destination is now cleared`
        : `Kept ${label} blocked`)
      refreshDetail(detail.id); refreshRules(); refreshSessions()
    } catch (e) {
      toast(`Failed: ${e.detail || e.message}`)
    } finally { setBusy(false) }
  }

  async function approve() {
    setBusy(true)
    try {
      await api(`/api/sandbox/sessions/${detail.id}/approve`, { method: 'POST' })
      toast('Session approved — staged work saved')
      selectedRef.current = null
      setSelected(null); setDetail(null)
      refreshSessions()
    } catch (e) {
      toast(`Approve failed: ${e.detail || e.message}`)
    } finally { setBusy(false) }
  }

  async function quarantine() {
    if (!window.confirm('Quarantine this session? Its staged work is discarded and the sandbox overlay is nuked.')) return
    setBusy(true)
    try {
      await api(`/api/sandbox/sessions/${detail.id}/quarantine`, { method: 'POST' })
      toast('Session quarantined — work discarded')
      selectedRef.current = null
      setSelected(null); setDetail(null)
      refreshSessions()
    } catch (e) {
      toast(`Quarantine failed: ${e.detail || e.message}`)
    } finally { setBusy(false) }
  }

  async function revoke(r) {
    setBusy(true)
    try {
      await api(`/api/sandbox/rules/${r.id}`, { method: 'DELETE' })
      toast(`Revoked ${r.dest}:${r.port} — future runs are blocked again`)
      refreshRules()
      if (selectedRef.current != null) refreshDetail(selectedRef.current)
    } catch (e) {
      toast(`Revoke failed: ${e.detail || e.message}`)
    } finally { setBusy(false) }
  }

  const shown = filter === 'undecided' ? sessions.filter((s) => !s.decided) : sessions
  const facts = detail?.facts || {}
  const beacons = detail?.beacons || []
  const behaviors = detail?.behavior || []
  // Actionable (still-blocked) rows sort first; allowed/delivered ones sink to the
  // bottom so the operator sees what still needs a decision. Stable within a group.
  const blockedFirst = (rows) =>
    rows.map((r, i) => [r, i])
      .sort((a, b) => ((b[0].status === 'blocked') - (a[0].status === 'blocked')) || (a[1] - b[1]))
      .map(([r]) => r)
  const wan = blockedFirst((detail?.egress || []).filter((r) => r.scope === 'wan'))
  const lan = blockedFirst((detail?.egress || []).filter((r) => r.scope === 'lan'))

  return (
    <div className="sbx-layout">
      <aside className="sbx-queue">
        <div className="sbx-newrun">
          <div className="side-title">New sandbox run</div>
          <div className="dim small sbx-newrun-help">
            Boots a fresh throwaway VM and runs this command with egress locked and
            every exec, DNS query and packet captured (~90s), then stages a report to
            review before anything goes live.
          </div>
          <select value={runProject} disabled={running}
                  onChange={(e) => setRunProject(e.target.value)}>
            {projects.length === 0 && <option value="">no projects</option>}
            {projects.map((p) => (
              <option key={p.slug} value={p.slug}>{p.name || p.slug}</option>
            ))}
          </select>
          <input value={runCmd} disabled={running} placeholder="python3 app.py"
                 onChange={(e) => setRunCmd(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter') startRun() }} />
          <button disabled={running || !runProject || !runCmd.trim()}
                  onClick={startRun}>
            {running ? 'Running…' : 'Run in sandbox'}</button>
          {runErr && <div className="error">{runErr}</div>}
        </div>

        <div className="row" style={{ margin: 0 }}>
          <div className="side-title grow">Sessions</div>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}
                  style={{ padding: '3px 6px', fontSize: 12 }}>
            <option value="all">all</option>
            <option value="undecided">undecided</option>
          </select>
        </div>
        <ul className="sbx-session-list">
          {shown.map((s) => (
            <li key={s.id}
                className={`sbx-session${selected === s.id ? ' active' : ''}`}
                onClick={() => openSession(s.id)}>
              <div className="sbx-session-top">
                <span className="grow ellipsis mono" title={s.command}>{s.command}</span>
                {(s.counts?.beacons_external ?? 0) > 0 &&
                  <span title="external beacon attempt">🛡</span>}
                <span className={`sbx-pill ${s.verdict}`}>{s.verdict}</span>
              </div>
              {s.headline && <div className="sbx-headline ellipsis">{s.headline}</div>}
              <div className="dim small ellipsis">
                {s.project} · {s.id} · {relTime(s.created_at)}
                {s.decided ? ' · decided' : ''}
              </div>
            </li>
          ))}
          {shown.length === 0 && (
            <li className="dim center-pad" style={{ cursor: 'default' }}>
              no sandbox sessions{filter === 'undecided' ? ' awaiting review' : ' yet'}
            </li>
          )}
        </ul>
      </aside>

      <main className="sbx-main">
        {!detail ? (
          <div className="dim center-pad">pick a session to review its evidence</div>
        ) : (
          <>
            <div className="sbx-card">
              <div className="sbx-verdict-top">
                <span className={`sbx-pill lg ${detail.verdict}`}>
                  {VERDICT_LABEL[detail.verdict] || detail.verdict}</span>
                {detail.rule && <span className="sbx-rule-chip">rule: {detail.rule}</span>}
                <span className="dim small">{detail.id}</span>
              </div>
              <div className="mono sbx-cmd">{detail.command}</div>
              {detail.headline && <p className="sbx-headline-lg">{detail.headline}</p>}
              <div className="sbx-tiles">
                <Tile label="DNS lookups" value={facts.dns ?? 0} />
                <Tile label="Egress attempts" value={facts.egress_dests ?? 0}
                      sub={`${facts.egress_new ?? 0} new`} />
                <Tile label="Blocked at tap" value={facts.blocked_attempts ?? 0}
                      bad={(facts.blocked_attempts ?? 0) > 0} />
                <Tile label="Delivered" value={fmtBytes(facts.delivered_bytes)} />
                <Tile label="Sensitive reads" value={facts.sensitive ?? 0}
                      bad={(facts.sensitive ?? 0) > 0} />
                <Tile label="Beacons" value={facts.beacons ?? 0}
                      bad={(facts.beacons_external ?? 0) > 0} />
                <Tile label="Processes" value={facts.execs ?? 0} />
                <Tile label="Behavioral flags" value={facts.behavior ?? 0}
                      bad={(facts.behavior ?? 0) > 0} />
              </div>
            </div>

            {behaviors.length > 0 && (
              <Section title="Behavioral flags">
                {behaviors.map((b, i) => (
                  <div key={i} className={`sbx-row sev-${b.sev || 'warn'}`}>
                    <div className="grow" style={{ minWidth: 0 }}>
                      <div className="ellipsis">
                        <span className="sbx-rule-chip">{b.kind}</span>{' '}
                        <span className="dim small">{b.rule}</span>
                      </div>
                      <div className="mono small ellipsis" title={b.evidence}>{b.evidence}</div>
                    </div>
                    <span className={`sbx-pill ${b.sev === 'crit' ? 'crit' : 'warn'}`}>
                      {b.sev === 'crit' ? 'critical' : 'suspicious'}</span>
                  </div>
                ))}
              </Section>
            )}

            {(detail.artifact || beacons.length > 0) && (
              <Section title="Artifact it wants you to open">
                {detail.artifact && (
                  <div className="sbx-row sev-ok">
                    <span className="mono grow ellipsis" title={detail.artifact}>
                      {detail.artifact}</span>
                  </div>
                )}
                {beacons.length === 0 ? (
                  <div className="sbx-row sev-ok">
                    <span className="grow" style={{ color: 'var(--green)' }}>
                      No network calls — safe to open.</span>
                    <span className="sbx-pill ok">clean</span>
                  </div>
                ) : (
                  beacons.map((b, i) => <BeaconRow key={i} b={b} />)
                )}
              </Section>
            )}

            {wan.length > 0 && (
              <Section title="Internet egress">
                {wan.map((r) => (
                  <EgressRow key={r.key} row={r} busy={busy} onDecide={decide} />
                ))}
              </Section>
            )}

            {lan.length > 0 && (
              <Section title="Local network">
                {lan.map((r) => (
                  <EgressRow key={r.key} row={r} busy={busy} onDecide={decide} />
                ))}
              </Section>
            )}

            {(detail.sensitive || []).length > 0 && (
              <Section title="Sensitive file & secret access">
                {detail.sensitive.map((f, i) => (
                  <div key={`${f.path}-${i}`} className={`sbx-row sev-${f.sev || 'ok'}`}>
                    <span className="mono grow ellipsis" title={f.path}>{f.path}</span>
                    {f.glob && <span className="tag">{f.glob}</span>}
                    <span className={`sbx-pill ${f.sev === 'ok' ? 'ok' : f.sev}`}>
                      {f.sev === 'ok' ? 'ok' : 'flagged'}</span>
                  </div>
                ))}
              </Section>
            )}

            {(detail.execs || []).length > 0 && (
              <Section title="Processes executed">
                {detail.execs.map((x, i) => (
                  <div key={i} className={`sbx-row sev-${x.sev || 'ok'}`}>
                    <span className="mono grow ellipsis" title={x.cmd}>{x.cmd}</span>
                    {x.sev !== 'ok' && (
                      <>
                        {x.rule && <span className="tag">{x.rule}</span>}
                        <span className={`sbx-pill ${x.sev}`}>{x.sev}</span>
                      </>
                    )}
                  </div>
                ))}
              </Section>
            )}

            {(detail.dns || []).length > 0 && (
              <Section title="DNS lookups">
                {detail.dns.map((d, i) => (
                  <div key={`${d.name}-${d.type}-${i}`} className="sbx-row sev-ok">
                    <span className="mono grow ellipsis">
                      {d.name} <span className="dim">({d.type})</span></span>
                    <span className={`sbx-pill ${d.new ? 'warn' : 'muted'}`}>
                      {d.new ? 'new' : 'seen'}</span>
                  </div>
                ))}
              </Section>
            )}

            {(detail.staged || []).length > 0 && (
              <Section title="Staged changes">
                {detail.staged.map((f) => (
                  <div key={f.path} className="sbx-row sev-ok">
                    <span className="mono grow ellipsis" title={f.path}>{f.path}</span>
                  </div>
                ))}
              </Section>
            )}
          </>
        )}
      </main>

      <aside className="sbx-rail">
        {detail && (
          <>
            <div className="side-title">Verdict</div>
            <button disabled={busy} onClick={approve}>Approve session &amp; save work</button>
            <button className="ghost danger" disabled={busy} onClick={quarantine}>
              Quarantine &amp; nuke</button>
            <p className="dim small" style={{ margin: 0 }}>
              Approve releases the staged files; quarantine discards them and the sandbox overlay.
            </p>
          </>
        )}

        <div className="side-title">Learned rules</div>
        <div className="dim small">
          {rules.length} destination{rules.length === 1 ? '' : 's'} trusted</div>
        <ul className="sbx-rules">
          {rules.map((r) => (
            <li key={r.id}>
              <div className="sbx-rule-line">
                <span className="mono grow ellipsis" title={`${r.dest}:${r.port}`}>
                  {r.dest}<span className="dim">:{r.port}</span></span>
                {r.scope && <span className="tag">{r.scope}</span>}
                <button className="win-btn" title="revoke — block again on future runs"
                        disabled={busy} onClick={() => revoke(r)}>✕</button>
              </div>
              {r.note && <div className="dim small ellipsis" title={r.note}>{r.note}</div>}
            </li>
          ))}
          {rules.length === 0 && <li className="dim small">no destinations trusted yet</li>}
        </ul>
        <p className="dim small sbx-prov" style={{ margin: 0 }}>
          Every verdict on this page is computed by deterministic rules over the
          captured evidence (nftables · dnsmasq · auditd · tcpdump · staging) —
          never a model.
        </p>

        {detail && (
          <>
            <div className="side-title">Session</div>
            <div className="sbx-meta">
              <div><span className="dim">project</span> <span className="mono">{detail.project}</span></div>
              <div><span className="dim">exit</span> <span className="mono">
                {detail.exit_status ?? '—'}{detail.timed_out ? ' · timed out' : ''}</span></div>
              <div><span className="dim">created</span> {relTime(detail.created_at)}</div>
              <div>
                {detail.egress_locked && <span className="tag">egress locked</span>}
                {detail.fresh && <span className="tag">fresh overlay</span>}
              </div>
            </div>
          </>
        )}
      </aside>

      {toasts.length > 0 && (
        <div className="sbx-toasts">
          {toasts.map((t) => <div key={t.id} className="sbx-toast">{t.msg}</div>)}
        </div>
      )}
    </div>
  )
}
