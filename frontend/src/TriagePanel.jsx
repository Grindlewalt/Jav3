import { useEffect, useState } from 'react'
import { api } from './api.js'
import { ts } from './format.js'
import { notifyError } from './notify.js'
import { Button, Toggle } from './components/index.js'

// Control strip for Auto review — the isolated triage reviewer
// (backend/reviewer.py; "triage" stays the internal name) — the first section
// of the Security page. It does not list the flagged
// hosts/alerts itself — those live once, as the ⚑ rows in the queue sections
// below. What remains: the Auto/Manual switch (beside the heading), a
// run-now button, the last
// sweep's tally, and the reviewer's recent autonomous approves/acks with
// one-click undo.
//
// It wears the same section head as the queues below it (sbx-sec-head → h3 +
// count chip + right-aligned actions) so the page reads as one list of things
// rather than a foreign card sitting on top of one. The explanatory paragraph
// is deliberately gone: it explained the feature every single visit, which is
// a one-time need.
//
// "Review now" only exists when something is untriaged — the sweeper keeps
// that at zero, so a permanently greyed button was the whole card's read.
//
// EVERY item string here — hostnames, the reviewer's own reasons (a model
// output derived from untrusted input) — is UNTRUSTED and is rendered as
// plain text nodes only, never through <Md>.

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
    catch (e) { notifyError(e) }
    setBusy(false)
    window.dispatchEvent(new Event('jarvis-files-changed'))
  }
  // optimistic: the thumb moves on the click, not a round trip later, and
  // snaps back if the PUT is refused
  async function toggle(on) {
    setS((p) => ({ ...p, enabled: on }))
    try { setS(await api('/api/reviewer', {
      method: 'PUT', body: JSON.stringify({ enabled: on }) })) }
    catch (e) { setS((p) => ({ ...p, enabled: !on })); notifyError(e) }
  }

  return (
    <section className="sbx-sec triage-card">
      <div className="sbx-sec-head">
        <h3>Auto review</h3>
        {/* the switch sits against the heading it names, not among the
            actions: "Auto review [on] Auto" reads as one setting */}
        <Toggle checked={!!s.enabled} onChange={toggle} label="Auto review"
                onText="Auto" offText="Manual"
                title={s.enabled
                  ? 'sweeps unreviewed queue items on its own every few '
                    + 'minutes — never during peak pricing'
                  : 'nothing is swept on its own — use Review now'} />
        {/* "clear" only when nothing is waiting on anyone: an item the
            reviewer flagged is reviewed, but it is not clear — it is waiting
            on the operator, so the flag count stands in for the chip */}
        {untriaged > 0 && <span className="sec-count">{untriaged} unreviewed</span>}
        {flagged > 0 && (
          <span className="tag triage-flag">{flagged} flagged below</span>)}
        {untriaged === 0 && flagged === 0 && (
          <span className="sec-count clear">clear</span>)}
        {(untriaged > 0 || s.running) && (
          <div className="sec-actions">
            <Button variant="ghost" disabled={busy || s.running}
                    title={`run auto review over the ${untriaged} unreviewed item(s) now`}
                    onClick={() => act('/api/reviewer/run')}>
              {s.running ? 'Running…' : 'Review now'}
            </Button>
          </div>
        )}
      </div>

      {last && (
        <div className="dim small triage-tally">
          Last sweep {ts(last.finished_at || last.started_at)}
          {' · '}{last.examined} seen{' · '}{last.allowed} allowed
          {' · '}{last.acked} acked{' · '}{last.flagged} flagged
          {last.error && ' · stopped early'}
        </div>
      )}

      {(s.recent_auto?.length || 0) > 0 && (
        <>
          <button className="triage-log-toggle" type="button"
                  onClick={() => setLogOpen((o) => !o)}>
            <span className={logOpen ? 'chev open' : 'chev'} aria-hidden="true">›</span>
            Auto-handled recently ({s.recent_auto.length}) — undoable
          </button>
          {logOpen && s.recent_auto.map((l) => (
            <div key={`l${l.id}`} className="sbx-row triage-row">
              <div className="grow" style={{ minWidth: 0 }}>
                <div className="ellipsis" title={l.subject}>
                  <span className="tag done">{l.action === 'approved' ? 'allowed' : 'acked'}</span>
                  {' '}<span className="mono">{l.subject}</span>
                  {l.project_slug && <span className="tag">{l.project_slug}</span>}
                </div>
                <div className="small dim ellipsis" title={l.reason}>
                  {ts(l.created_at)} · {l.reason}</div>
              </div>
              <Button variant="ghost" title="undo this auto-action" disabled={busy}
                      onClick={() => act(`/api/reviewer/log/${l.id}/undo`)}>Undo</Button>
            </div>
          ))}
        </>
      )}
    </section>
  )
}
