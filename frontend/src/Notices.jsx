import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { isWatched } from './agentWatch.js'
import { api, subscribeSse } from './api.js'

// Top-right notices — the successor to the nav bell and shield. Anything that
// needs operator eyes arrives as a desktop-style alert (red = critical
// security, amber = the rest) and clicks through to the Review Center, which
// is the actual ledger; the count badge on the Review nav link means a missed
// toast is never lost. The bar under each card drains its lifetime away and
// pauses while hovered (dismissal rides the CSS animation's end, so the pause
// is free). At most three cards show; the backlog folds into a "+N" chip.
//
// Sources: the live security SSE stream (instant, id-deduped), the agent-run
// notice stream, and the /api/notifications poll (egress/git/schedule deltas
// between snapshots). Security alerts are deliberately NOT toasted from the
// poll — the stream owns them, and a reconnect gap only costs a toast, never
// the badge count.
//
// Agent notices cover DEDICATED agents the operator started and then left: the
// run is detached server-side, so it finishes either way, and this is how they
// hear about it. A run whose panel is on screen in a visible tab is skipped
// (agentWatch) — they are already watching it — and the agents Jarvis spawns
// mid-turn never reach this stream at all.
//
// Every string shown here (summaries, commit messages, schedule names) is
// UNTRUSTED model/guest output — rendered as plain text nodes only.

let seq = 0

export function useNotices(enabled) {
  const [toasts, setToasts] = useState([])
  const [count, setCount] = useState(0)
  const prev = useRef(null)      // last /api/notifications snapshot
  const seen = useRef(new Set()) // security event ids already toasted

  const push = useCallback((t) => {
    setToasts((ts) => [...ts, { id: `n${seq++}`, sev: 'warn', ...t }])
  }, [])
  const dismiss = useCallback((id) => {
    setToasts((ts) => ts.filter((t) => t.id !== id))
  }, [])
  const clear = useCallback(() => setToasts([]), [])

  useEffect(() => {
    if (!enabled) return
    const load = async () => {
      let d
      try { d = await api('/api/notifications') } catch { return }
      setCount(d.count || 0)
      const p = prev.current
      prev.current = d
      if (!p) {
        // first snapshot: one quiet summary instead of a card per backlog item
        if (d.count > 0) push({
          title: `${d.count} item${d.count === 1 ? '' : 's'} waiting in review`,
          body: 'security alerts, approvals and requests', life: 10,
        })
        return
      }
      const newEgress = (d.egress_pending || 0) - (p.egress_pending || 0)
      if (newEgress > 0) push({
        title: `${newEgress} new host approval${newEgress === 1 ? '' : 's'}`,
        body: 'the guest asked to reach new egress hosts',
      })
      const oldGit = new Set((p.git || []).map((g) => g.id))
      ;(d.git || []).filter((g) => !oldGit.has(g.id)).forEach((g) => push({
        title: `commit request · ${g.project}`, body: g.message,
      }))
      const oldSched = new Set((p.schedules || []).map((s) => s.id))
      ;(d.schedules || []).filter((s) => !oldSched.has(s.id)).forEach((s) => push({
        title: `proposed schedule · ${s.name}`,
        body: s.kind === 'agent' ? s.agent_slug : 'jarvis',
      }))
    }
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [enabled, push])

  useEffect(() => {
    if (!enabled) return
    return subscribeSse('/api/security/stream', (ev) => {
      if (ev.type !== 'security_event' || seen.current.has(ev.id)) return
      seen.current.add(ev.id)
      setCount((c) => c + 1)  // the next poll re-syncs the real total
      const crit = ['critical', 'crit'].includes(String(ev.severity || '').toLowerCase())
      // eventId deep-links the card to that alert's evidence board, so the one
      // click a draining toast gets lands on the detail rather than the queue
      push({ sev: crit ? 'crit' : 'warn', eventId: ev.id,
             title: `security · ${ev.kind || 'alert'}`
                    + (ev.project_slug || ev.project ? ` · ${ev.project_slug || ev.project}` : ''),
             body: ev.summary || '' })
    })
  }, [enabled, push])

  // ...and the app's own fire-and-forget messages (see notify.js). These are
  // marked `local`: they are not queue items, so they must not click through
  // to the Review Center the way every other card does.
  useEffect(() => {
    if (!enabled) return
    const onNotice = (e) => push({ ...(e.detail || {}), local: true })
    window.addEventListener('jarvis-notice', onNotice)
    return () => window.removeEventListener('jarvis-notice', onNotice)
  }, [enabled, push])

  useEffect(() => {
    if (!enabled) return
    return subscribeSse('/api/agents/notices/stream', (ev) => {
      if (ev.type !== 'agent_run_done') return
      if (isWatched(ev.conversation_id)) return   // they're looking right at it
      push({
        sev: ev.ok ? 'ok' : 'crit',
        project: ev.project,
        title: `${ev.agent || 'agent'} ${ev.ok ? 'finished' : 'failed'}`
               + (ev.took ? ` · ${ev.took}` : ''),
        body: ev.ok ? (ev.summary || 'no output') : (ev.error || 'run failed'),
        life: 14,   // longer than an approval card: this is a result to read
      })
    })
  }, [enabled, push])

  return { toasts, count, dismiss, clear }
}

export default function Notices({ toasts, dismiss, clear }) {
  const navigate = useNavigate()
  if (toasts.length === 0) return null
  // Which three survive. Straight `slice(-3)` meant a burst of "save failed"
  // could push a critical security card off the screen — a UI convenience
  // degrading the security notification path. Evict by rank first, recency
  // second; render the survivors in arrival order.
  const rank = (t) => (t.sev === 'crit' ? 2 : t.local ? 0 : 1)
  const keep = new Set(
    toasts.map((t, i) => ({ t, i }))
      .sort((a, b) => rank(a.t) - rank(b.t) || a.i - b.i)
      .slice(-3)
      .map(({ t }) => t.id))
  const shown = toasts.filter((t) => keep.has(t.id))
  const extra = toasts.length - shown.length
  const open = (t) => {
    dismiss(t.id)
    // an agent notice belongs to the board it ran on; everything else is a
    // queue item and belongs in the Review Center
    if (t.project) navigate(`/projects/${t.project}`)
    else navigate('/review', t.eventId ? { state: { openEvent: t.eventId } } : undefined)
  }
  return (
    <div className="notices" role="status" aria-live="polite">
      {shown.map((t) => (t.local ? (
        // the app talking about what just happened, not a queue item: there is
        // nothing to open, so it is not a button and clicking it navigates
        // nowhere. It also wraps instead of ellipsing — a failure reason the
        // operator cannot finish reading is the same as no message.
        <div key={t.id} className={`notice ${t.sev || 'warn'} local`} role="alert">
          <span className="notice-head">
            <span className="notice-dot" aria-hidden="true" />
            <span className="notice-title">{t.title}</span>
            <button type="button" className="notice-x" aria-label="dismiss"
                    onClick={() => dismiss(t.id)}>✕</button>
          </span>
          {t.body && <span className="notice-body">{t.body}</span>}
          <span className="notice-bar"
                style={t.life ? { '--n-life': `${t.life}s` } : undefined}
                onAnimationEnd={() => dismiss(t.id)} />
        </div>
      ) : (
        <button key={t.id} type="button" className={`notice ${t.sev}`}
                title={t.project ? `open the ${t.project} board`
                  : t.eventId ? 'open the evidence for this alert'
                    : 'open the Review Center'}
                onClick={() => open(t)}>
          <span className="notice-head">
            <span className="notice-dot" aria-hidden="true" />
            <span className="notice-title ellipsis">{t.title}</span>
          </span>
          {t.body && <span className="notice-body">{t.body}</span>}
          <span className="notice-bar"
                style={t.life ? { '--n-life': `${t.life}s` } : undefined}
                onAnimationEnd={() => dismiss(t.id)} />
        </button>
      )))}
      {extra > 0 && (
        <button type="button" className="notice-more"
                onClick={() => { clear(); navigate('/review') }}>
          +{extra} more — open review
        </button>
      )}
    </div>
  )
}
