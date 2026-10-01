import { useEffect, useState } from 'react'
import { api } from './api.js'
import SecurityBoard from './SecurityBoard.jsx'
import { sevClass, ts } from './format.js'

// The Security log's recent past, acknowledged rows included: the one place a
// row filed already acknowledged ("by you", or a kind set to Record only) can be
// found, since it never reaches the queue or a toast. Collapsed until opened.
// Every string is the server's or an agent's: plain text nodes only.

const FILTERS = [
  ['all', 'Everything'],
  ['you', 'By you'],
  ['recorded', 'Recorded only'],
  ['rule', 'Filtered as normal work'],
]

// "Filtered as normal work": a rule judged the event routine (a scratch file the
// run made and threw away, a known device reconnecting, a process the agent's own
// run_code started). Recorded, acknowledged, never in the Queue; `rule` says why.
const keep = (filter) => (e) => (
  filter === 'you' ? e.actor === 'operator'
    : filter === 'recorded' ? e.quiet === 'kind'
      : filter === 'rule' ? e.quiet === 'rule'
        : true)

export default function SecurityHistory() {
  const [open, setOpen] = useState(false)
  const [rows, setRows] = useState(null)
  const [filter, setFilter] = useState('all')
  const [board, setBoard] = useState(null)

  useEffect(() => {
    if (!open) return
    let live = true
    api('/api/security/events?limit=200')
      .then((r) => { if (live) setRows(r.events || []) })
      .catch(() => { if (live) setRows([]) })
    return () => { live = false }
  }, [open])

  const shown = (rows || []).filter(keep(filter))
  return (
    <details className="sec-history" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>History</summary>
      {open && (
        <>
          <div className="sec-history-filter" role="group" aria-label="Filter the history">
            {FILTERS.map(([v, label]) => (
              <button key={v} type="button" className={filter === v ? 'ghost on' : 'ghost'}
                      aria-pressed={filter === v} onClick={() => setFilter(v)}>{label}</button>
            ))}
          </div>
          {rows === null && <p className="dim small">Loading…</p>}
          {rows !== null && shown.length === 0 && (
            <p className="dim small">Nothing here yet.</p>)}
          {shown.map((e) => (
            <div key={e.id} className={`sbx-row sev-${sevClass(e.severity)} sec-history-row`}>
              <div className="grow rev-alert-main">
                <div className="sbx-verdict-top rev-alert-top">
                  <span className={`tag sev-${sevClass(e.severity)}-tag`}>{e.severity}</span>
                  <span className="mono small">{e.kind}</span>
                  {e.project_slug && <span className="tag">{e.project_slug}</span>}
                  {e.count > 1 && <span className="tag">×{e.count}</span>}
                  {e.actor === 'operator' && <span className="tag by-you">by you</span>}
                  {e.quiet === 'kind' && <span className="tag">recorded only</span>}
                  {e.quiet === 'rule' && <span className="tag" title={e.rule || ''}>normal work</span>}
                  {!e.acknowledged && <span className="tag">waiting</span>}
                  <span className="dim small">{ts(e.count > 1 && e.last_seen ? e.last_seen : e.created_at)}</span>
                </div>
                <button type="button" className="rev-alert-open" onClick={() => setBoard(e)}
                        title="open the evidence board">
                  <span className="rev-alert-summary">{e.summary}</span>
                </button>
                {e.quiet === 'rule' && e.rule && <div className="dim small">{e.rule}</div>}
              </div>
            </div>
          ))}
        </>
      )}
      {board && <SecurityBoard eventId={board.id} seed={board} onClose={() => setBoard(null)} />}
    </details>
  )
}
