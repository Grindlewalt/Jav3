import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'

// The evidence board for one security alert. A card in the Review Center says
// "write flag: network_call in fetch.py"; this is where the operator finds out
// whether that matters — the flagged lines in place, the diff against git HEAD,
// the directory the file lives in, the traffic behind an egress cut, the
// history behind a login burst. Assembled server-side by backend/secctx.py and
// shipped as TYPED SECTIONS (facts | code | diff | files | table | note), so a
// new alert kind gets a useful board with no change here.
//
// EVERYTHING on this board is UNTRUSTED: agent-written source, scanned
// hostnames, guest request paths, attempted usernames. It is rendered as plain
// text nodes only — no <Md>, no dangerouslySetInnerHTML, ever.

function fmtBytes(n) {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`
  return `${(n / 1048576).toFixed(1)} MB`
}

const SEV = { info: 'info', warn: 'warn', warning: 'warn', critical: 'crit', crit: 'crit' }
const sevClass = (s) => SEV[String(s || 'info').toLowerCase()] || 'info'

function Facts({ s }) {
  return (
    <dl className="sbd-facts">
      {s.rows.map((r, i) => (
        <div key={i} className="sbd-fact">
          <dt>{r.label}</dt>
          <dd>
            {r.value}
            {r.hint && <span className="sbd-hint">{r.hint}</span>}
          </dd>
        </div>
      ))}
    </dl>
  )
}

// Line-numbered source with the flagged lines marked. The gutter is a separate
// column so selecting the code doesn't drag the numbers along with it.
function Code({ s }) {
  return (
    <div className="sbd-code">
      {s.path && <div className="sbd-code-path mono">{s.path}</div>}
      {s.blocks.map((b, i) => (
        <div key={i} className="sbd-block">
          {i > 0 && <div className="sbd-gap">⋯</div>}
          {b.lines.map((ln) => (
            <div key={ln.n} className={ln.mark ? 'sbd-line mark' : 'sbd-line'}>
              <span className="sbd-ln" aria-hidden="true">{ln.n}</span>
              <code>{ln.text || ' '}</code>
            </div>
          ))}
        </div>
      ))}
      {s.truncated && <div className="dim small sbd-trunc">…snippet truncated</div>}
    </div>
  )
}

function Diff({ s }) {
  return (
    <div className="sbd-code sbd-diff">
      {s.hunks.map((h, i) => (
        <div key={i} className="sbd-block">
          <div className="sbd-hunk mono">{h.header}</div>
          {h.lines.map((ln, j) => (
            <div key={j} className={`sbd-line d-${ln.k === '+' ? 'add' : ln.k === '-' ? 'del' : 'ctx'}`}>
              <span className="sbd-ln" aria-hidden="true">{ln.k === ' ' ? '' : ln.k}</span>
              <code>{ln.text || ' '}</code>
            </div>
          ))}
        </div>
      ))}
      {s.truncated && <div className="dim small sbd-trunc">…diff truncated</div>}
    </div>
  )
}

// The directory the flagged file sits in. `subject` is the file itself,
// `burst` means it was touched in the same write turn (the real blast radius),
// `flagged` means it carries an alert of its own.
function Files({ s }) {
  const box = useRef(null)
  const subject = useRef(null)
  // A real directory can be 60+ files, and the flagged one is wherever the
  // alphabet put it. The list scrolls inside its own box (so it never pushes
  // the diff off-screen) and opens parked on the subject — set directly rather
  // than via scrollIntoView, which would also scroll the modal body.
  useEffect(() => {
    if (box.current && subject.current) {
      box.current.scrollTop = Math.max(0, subject.current.offsetTop - 60)
    }
  }, [s])
  return (
    <ul className="sbd-files" ref={box}>
      {s.entries.map((e) => (
        <li key={e.rel} className={e.subject ? 'subject' : ''}
            ref={e.subject ? subject : undefined}>
          <span className="sbd-fname mono">
            {e.kind === 'dir' ? '📁 ' : ''}{e.name}{e.kind === 'dir' ? '/' : ''}
          </span>
          {e.subject && <span className="tag sbd-t-subject">this file</span>}
          {e.burst && <span className="tag sbd-t-burst">same write</span>}
          {e.flagged && <span className="tag sbd-t-flagged">⚑ own alert</span>}
          <span className="grow" />
          <span className="dim small">
            {e.kind === 'dir'
              ? (e.count != null ? `${e.count} item${e.count === 1 ? '' : 's'}` : '')
              : fmtBytes(e.size)}
          </span>
          <span className="dim small sbd-when">{e.ago}</span>
        </li>
      ))}
    </ul>
  )
}

function Table({ s }) {
  if (!s.rows.length) {
    return <div className="dim small">{s.empty || 'nothing recorded'}</div>
  }
  return (
    <div className="sbd-tablewrap">
      <table className="sbd-table">
        <thead>
          <tr>{s.cols.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {s.rows.map((r, i) => (
            <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Section({ s }) {
  const body = s.type === 'facts' ? <Facts s={s} />
    : s.type === 'code' ? <Code s={s} />
      : s.type === 'diff' ? <Diff s={s} />
        : s.type === 'files' ? <Files s={s} />
          : s.type === 'table' ? <Table s={s} />
            : <div className="sbd-notebody">{s.text}</div>
  return (
    <section className="sbd-sec">
      {s.title && <h4>{s.title}</h4>}
      {body}
      {s.note && <div className="dim small sbd-note">{s.note}</div>}
    </section>
  )
}

export default function SecurityBoard({ eventId, seed, onClose, onAck }) {
  const [board, setBoard] = useState(null)
  const [err, setErr] = useState(null)
  const [raw, setRaw] = useState(false)

  useEffect(() => {
    let live = true
    setBoard(null); setErr(null)
    api(`/api/security/events/${eventId}/context`)
      .then((b) => { if (live) setBoard(b) })
      .catch((e) => { if (live) setErr(e.detail || String(e)) })
    return () => { live = false }
  }, [eventId])

  // Escape closes. The scrim handles the click-away; this is the keyboard half.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const ev = board?.event || seed || {}
  const sev = sevClass(ev.severity)

  return (
    <div className="sbd-scrim" onClick={onClose}>
      <div className={`sbd-modal sev-${sev}`} onClick={(e) => e.stopPropagation()}
           role="dialog" aria-modal="true" aria-label="security alert detail">
        <div className="sbd-head">
          <span className={`tag sev-${sev}-tag`}>{ev.severity || '…'}</span>
          <div className="sbd-headtext">
            <strong className="sbd-title">{board?.title || ev.summary || 'Loading…'}</strong>
            <div className="dim small sbd-sub">
              <span className="mono">{ev.kind}</span>
              {ev.project_slug && <> · {ev.project_slug}</>}
              {ev.created_at && <> · {String(ev.created_at).replace('T', ' ').slice(0, 19)}</>}
              {ev.acknowledged ? <> · acknowledged</> : null}
            </div>
          </div>
          <button className="ghost" onClick={onClose} title="close (Esc)">✕</button>
        </div>

        <div className="sbd-body">
          {err && <div className="sbd-err">could not load this board — {err}</div>}
          {!board && !err && <div className="dim center-pad">assembling…</div>}

          {board && (board.what || board.why) && (
            <section className="sbd-brief">
              {board.what && <p className="sbd-what">{board.what}</p>}
              {board.why && <p className="sbd-why">{board.why}</p>}
              {board.checks?.length > 0 && (
                <>
                  <div className="sbd-checkhead">What to check</div>
                  <ul className="sbd-checks">
                    {board.checks.map((c, i) => <li key={i}>{c}</li>)}
                  </ul>
                </>
              )}
            </section>
          )}

          {board?.sections.map((s, i) => <Section key={i} s={s} />)}

          {board && (
            <section className="sbd-sec">
              <button className="ghost small" onClick={() => setRaw((r) => !r)}>
                {raw ? 'hide' : 'show'} the raw event
              </button>
              {raw && (
                <pre className="log-pre sbd-raw">
                  {JSON.stringify({ ...ev, detail: board.detail }, null, 2)}
                </pre>
              )}
            </section>
          )}
        </div>

        <div className="sbd-foot">
          <span className="dim small">
            {ev.triage_verdict === 'flag' ? `⚑ triage: ${ev.triage_reason || 'flagged'}` : ''}
          </span>
          <span className="grow" />
          <button className="ghost" onClick={onClose}>Close</button>
          {onAck && !ev.acknowledged && (
            <button onClick={() => { onAck(ev.id); onClose() }}>Acknowledge</button>
          )}
        </div>
      </div>
    </div>
  )
}
