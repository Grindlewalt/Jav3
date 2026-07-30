import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, subscribeSse } from './api.js'

// Top-right notices — the successor to the nav bell and shield. Anything that
// needs operator eyes arrives as a desktop-style alert (red = critical
// security, amber = the rest) and clicks through to the Review Center, which
// is the actual ledger; the count badge on the Review nav link means a missed
// toast is never lost. The bar under each card drains its lifetime away and
// pauses while hovered (dismissal rides the CSS animation's end, so the pause
// is free). At most three cards show; the backlog folds into a "+N" chip.
//
// Sources: the live security SSE stream (instant, id-deduped) and the
// /api/notifications poll (egress/git/schedule deltas between snapshots).
// Security alerts are deliberately NOT toasted from the poll — the stream owns
// them, and a reconnect gap only costs a toast, never the badge count.
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

  return { toasts, count, dismiss, clear }
}

export default function Notices({ toasts, dismiss, clear }) {
  const navigate = useNavigate()
  if (toasts.length === 0) return null
  const shown = toasts.slice(-3)
  const extra = toasts.length - shown.length
  const open = (t) => {
    dismiss(t.id)
    navigate('/review', t.eventId ? { state: { openEvent: t.eventId } } : undefined)
  }
  return (
    <div className="notices" role="status" aria-live="polite">
      {shown.map((t) => (
        <button key={t.id} type="button" className={`notice ${t.sev}`}
                title={t.eventId ? 'open the evidence for this alert'
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
      ))}
      {extra > 0 && (
        <button type="button" className="notice-more"
                onClick={() => { clear(); navigate('/review') }}>
          +{extra} more — open review
        </button>
      )}
    </div>
  )
}
