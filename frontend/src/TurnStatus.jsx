import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { fmtCost, fmtElapsed, fmtMs, streamingIndex, toolLine, turnStats } from './turnEvents.js'

const COST_POLL_MS = 5000

// One steady line while a turn runs, in the composer's dock where nothing
// scrolls or reflows under it:
//   ● working 2m 13s · 14 tools · $0.02          now: run  ls -l  12s
// The clock and the counts are this tab's view of the turn (a re-attached
// turn counts from the moment it re-attached). The cost is the conversation's
// spend since the turn began, polled from /info: the stream carries no cost.
// Only this component ticks; the transcript does not re-render each second.
export default function TurnStatus({ busy, messages, cid, waiting = false, compact = false }) {
  const [now, setNow] = useState(() => Date.now())
  const seen = useRef(null)                 // when this tab first saw the turn
  const [cost, setCost] = useState(null)
  const base = useRef(null)

  useEffect(() => {
    if (!busy) { seen.current = null; return undefined }
    if (seen.current == null) seen.current = Date.now()
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [busy])

  // the spend so far: the first reading is the baseline (the turn has only just
  // begun), later ones minus it. A chat without a saved row has no /info.
  useEffect(() => {
    if (!busy || cid == null) { base.current = null; setCost(null); return undefined }
    let live = true
    const read = () => api(`/api/conversations/${cid}/info`).then((r) => {
      if (!live || typeof r?.cost_usd !== 'number') return
      if (base.current == null) base.current = r.cost_usd
      setCost(Math.max(0, r.cost_usd - base.current))
    }).catch(() => {})
    read()
    const t = setInterval(read, COST_POLL_MS)
    return () => { live = false; clearInterval(t) }
  }, [busy, cid])

  if (!busy) return null
  const i = streamingIndex(messages)
  const m = i === -1 ? null : messages[i]
  const start = m?.t0 ?? seen.current ?? now
  const st = turnStats(m?.parts)
  const cur = st.current ? toolLine(st.current.name, st.current.args) : null
  const facts = [
    `${st.tools} tool${st.tools === 1 ? '' : 's'}`,
    ...(cost != null ? [fmtCost(cost)] : []),
  ]
  return (
    <div className={`turn-status${waiting ? ' waiting' : ''}${compact ? ' compact' : ''}`}
         data-testid="turn-status">
      <span className="turn-status-dot" aria-hidden="true" />
      <span className="turn-status-main">
        {waiting ? 'waiting for you' : 'working'} {fmtElapsed(now - start)}
        <span className="turn-status-dim"> · {facts.join(' · ')}</span>
        {st.failed > 0 && <span className="turn-status-bad"> · {st.failed} failed</span>}
      </span>
      {cur && !waiting && (
        <span className="turn-status-now ellipsis" title={cur.arg}>
          now: {cur.title}{cur.arg ? `  ${cur.arg}` : ''}
          {st.current.t0 != null && now - st.current.t0 >= 2000
            ? `  ${fmtMs(now - st.current.t0)}` : ''}
        </span>
      )}
    </div>
  )
}
