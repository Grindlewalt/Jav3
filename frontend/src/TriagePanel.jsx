import { useEffect, useState } from 'react'
import { api } from './api.js'

// Control strip for the isolated triage reviewer (backend/reviewer.py), a card
// at the top of the Review Center. It no longer lists the flagged hosts/alerts
// itself — those live once, as the ⚑ rows in the queue sections below (listing
// them here too made the page read as two egress queues). What remains: the
// auto-sweep switch, a run-now button, the last run's tally, and the
// reviewer's recent autonomous approves/acks with one-click undo.
//
// EVERY item string here — hostnames, the reviewer's own reasons (a model
// output derived from untrusted input) — is UNTRUSTED and is rendered as
// plain text nodes only, never through <Md>.

function ts(s) { return s ? String(s).replace('T', ' ').slice(5, 16) : '' }

export default function TriagePanel() {
  const [s, setS] = useState(null)
  const [busy, setBusy] = useState(false)
  const [logOpen, setLogOpen] = useState(false)

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
          {flagged > 0 && <span className="tag triage-flag"> {flagged} ⚑ below</span>}</span>
        <label className="small dim triage-toggle"
               title="when on, the reviewer sweeps new queue items on its own every few minutes">
          <input type="checkbox" checked={!!s.enabled}
                 onChange={(e) => toggle(e.target.checked)} /> auto-triage
        </label>
        <button className="ghost" disabled={busy || s.running || untriaged === 0}
                title={untriaged ? `run the reviewer over the ${untriaged} untriaged item(s) now`
                                 : 'nothing untriaged — the queue below is already sorted'}
                onClick={() => act('/api/reviewer/run')}>
          {s.running ? 'running…' : 'Triage now'}
        </button>
      </div>
      <div className="notif-item triage-sub">
        <span className="grow small dim">
          An isolated reviewer clears routine queue items itself and ⚑-flags the
          rest for your eyes below.
          {' '}{untriaged > 0 ? `${untriaged} untriaged.` : 'Queue triaged.'}
          {last && ` Last run: ${last.examined} seen, ${last.allowed} allowed, `
            + `${last.acked} acked, ${last.flagged} flagged.`}
          {last?.error && ' Stopped early.'}
        </span>
      </div>

      {(s.recent_auto?.length || 0) > 0 && (
        <>
          <button className="triage-log-toggle" type="button"
                  onClick={() => setLogOpen((o) => !o)}>
            <span className={logOpen ? 'chev open' : 'chev'} aria-hidden="true">›</span>
            auto-handled recently ({s.recent_auto.length}) — undoable
          </button>
          {logOpen && s.recent_auto.map((l) => (
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
