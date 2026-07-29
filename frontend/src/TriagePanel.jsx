import { useEffect, useState } from 'react'
import { api } from './api.js'

// Control surface for the isolated triage reviewer (backend/reviewer.py),
// rendered as a card at the top of the Review Center (it used to be a nav
// dropdown behind a shield icon): the auto-sweep toggle, a run-now button,
// what the reviewer flagged for human eyes (with inline approve/reject/ack),
// and its recent autonomous approves/acks with one-click undo.
//
// EVERY item string here — hostnames, alert summaries, the reviewer's own
// reasons (a model output derived from untrusted input) — is UNTRUSTED and is
// rendered as plain text nodes only, never through <Md>.

function ts(s) { return s ? String(s).replace('T', ' ').slice(5, 16) : '' }

export default function TriagePanel() {
  const [s, setS] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = () => api('/api/reviewer').then(setS).catch(() => {})
  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])
  // poll faster while a run is in flight so the summary lands promptly
  useEffect(() => {
    if (!s?.running) return
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [s?.running])

  if (!s) return null
  const flagged = (s.flagged_hosts?.length || 0) + (s.flagged_alerts?.length || 0)
  const untriaged = (s.untriaged?.hosts || 0) + (s.untriaged?.alerts || 0)
  const last = s.last_run

  async function act(path) {
    setBusy(true)
    try { await api(path, { method: 'POST' }); await load() }
    catch (e) { window.alert(e.detail || String(e)) }
    setBusy(false)
    window.dispatchEvent(new Event('jarvis-files-changed'))
  }
  async function toggle(on) {
    try { setS(await api('/api/reviewer', {
      method: 'PUT', body: JSON.stringify({ enabled: on }) })) }
    catch (e) { window.alert(e.detail || String(e)) }
  }

  return (
    <div className="sbx-card triage-card">
          <div className="notif-item triage-head">
            <span className="grow"><b>Triage reviewer</b>
              {flagged > 0 && <span className="tag triage-flag"> {flagged} flagged</span>}</span>
            <label className="small dim triage-toggle" title="sweep the queues automatically">
              <input type="checkbox" checked={!!s.enabled}
                     onChange={(e) => toggle(e.target.checked)} /> auto
            </label>
            <button className="ghost" disabled={busy || s.running || untriaged === 0}
                    title={untriaged ? `triage ${untriaged} untriaged item(s) now` : 'queue is clear'}
                    onClick={() => act('/api/reviewer/run')}>
              {s.running ? 'running…' : '▶ run'}
            </button>
          </div>
          <div className="notif-item triage-sub">
            <span className="grow small dim">
              {untriaged > 0 ? `${untriaged} untriaged` : 'queue triaged'}
              {last && ` · last run: ${last.examined} seen, ${last.allowed} allowed, `
                + `${last.acked} acked, ${last.flagged} flagged`}
              {last?.error && ' · stopped early'}
            </span>
          </div>

          {flagged === 0 && untriaged === 0 && (
            <div className="dim small notif-empty">nothing needs your eyes</div>
          )}

          {(s.flagged_hosts?.length || 0) > 0 && (
            <>
              <div className="triage-sec small dim">flagged hosts</div>
              {s.flagged_hosts.map((h) => (
                <div key={`h${h.id}`} className="notif-item triage-row">
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="ellipsis mono small" title={h.host}>
                      {h.host} {h.project_slug && <span className="dim">· {h.project_slug}</span>}
                    </div>
                    <div className="small dim ellipsis" title={h.triage_reason}>⚑ {h.triage_reason}</div>
                  </div>
                  <button className="win-btn ok" title="approve host" disabled={busy}
                          onClick={() => act(`/api/egress/pending/${h.id}/approve`)}>✓</button>
                  <button className="win-btn" title="reject host" disabled={busy}
                          onClick={() => act(`/api/egress/pending/${h.id}/reject`)}>✕</button>
                </div>
              ))}
            </>
          )}

          {(s.flagged_alerts?.length || 0) > 0 && (
            <>
              <div className="triage-sec small dim">flagged alerts</div>
              {s.flagged_alerts.map((a) => (
                <div key={`a${a.id}`} className="notif-item triage-row">
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="ellipsis small" title={a.summary}>
                      <span className="mono">{a.kind}</span> · {a.summary}
                    </div>
                    <div className="small dim ellipsis" title={a.triage_reason}>⚑ {a.triage_reason}</div>
                  </div>
                  <button className="win-btn" title="acknowledge" disabled={busy}
                          onClick={() => act(`/api/security/events/${a.id}/ack`)}>✓</button>
                </div>
              ))}
            </>
          )}

          {(s.recent_auto?.length || 0) > 0 && (
            <>
              <div className="triage-sec small dim">auto-handled (undoable)</div>
              {s.recent_auto.map((l) => (
                <div key={`l${l.id}`} className="notif-item triage-row">
                  <div className="grow" style={{ minWidth: 0 }}>
                    <div className="ellipsis small" title={l.subject}>
                      {l.action === 'approved' ? '✓ allowed' : '✓ acked'}{' '}
                      <span className="mono">{l.subject}</span>
                      {l.project_slug && <span className="dim"> · {l.project_slug}</span>}
                    </div>
                    <div className="small dim ellipsis" title={l.reason}>
                      {ts(l.created_at)} · {l.reason}</div>
                  </div>
                  <button className="ghost" title="undo this auto-action" disabled={busy}
                          onClick={() => act(`/api/reviewer/log/${l.id}/undo`)}>undo</button>
                </div>
              ))}
            </>
          )}
    </div>
  )
}
