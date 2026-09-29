import { memo, useEffect, useState } from 'react'
import JobTree from './JobTree.jsx'
import Md from './Md.jsx'
import { isKnownModel, useModel } from './modelInfo.js'
import { PATHS } from './nav.jsx'
import {
  applyTurnEvent, clipText, errorLine, exitNote, finishTurn, fmtElapsed, fmtMs, foldParts,
  toolLine,
} from './turnEvents.js'

// Live tool-activity rendering shared by Chat and ChatBox: one-line rows
// (icon, title, the argument that matters, how long it took) that update as
// results land, with click-to-expand args/result. The folding of events into a
// message lives in turnEvents.js (pure, tested); re-exported here for the
// surfaces that import it from this file.
// SECURITY: tool args and results are UNTRUSTED text (they can contain web
// content) — they only ever render inside <pre>, never through <Md>.
export { applyTurnEvent, finishTurn }

// Line glyphs for the activity rows, drawn like the nav's (24-unit box,
// round caps, currentColor) so a tool row reads as part of the same app. The
// rows used to lead with colour emoji, which rendered in a different style on
// every platform and shouted over the prose around them. A glyph that already
// exists in the nav is borrowed, not redrawn.
const GLYPHS = {
  search: <><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.5 4.5" /></>,
  page: <path d="M6 3.5h8l4 4v13H6ZM14 3.5v4h4M9 12.5h6M9 16h6" />,
  folder: PATHS.projects,
  write: <path d="M4.5 19.5 5.6 15 15.4 5.2a2 2 0 0 1 2.8 0l.6.6a2 2 0 0 1 0 2.8L9
                  18.4ZM13.6 7l3.4 3.4" />,
  agent: PATHS.agents,
  tree: <><circle cx="12" cy="5" r="2" /><circle cx="6" cy="19" r="2" />
          <circle cx="18" cy="19" r="2" /><path d="M12 7v4.5M6 17v-5.5h12V17" /></>,
  research: <path d="M9.5 3.5h5M10.5 3.5v5.2L5 18.3a1.5 1.5 0 0 0 1.3 2.2h11.4a1.5 1.5 0
                     0 0 1.3-2.2l-5.5-9.6V3.5M7.4 14.5h9.2" />,
  map: <path d="M3.5 6.5 9 4l6 2.5L20.5 4v13.5L15 20l-6-2.5-5.5 2.5ZM9 4v13.5M15 6.5V20" />,
  schedule: PATHS.schedules,
  book: PATHS.memory,
  todo: <><rect x="4" y="4" width="16" height="16" rx="3" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>,
  branch: <><circle cx="6.5" cy="5.5" r="2" /><circle cx="6.5" cy="18.5" r="2" />
            <circle cx="17.5" cy="8" r="2" /><path d="M6.5 7.5v9M17.5 10c0 4-5 3.5-9.6 7" /></>,
  chart: <path d="M4 20h16M7 16.5v-5M12 16.5V7M17 16.5V10" />,
  run: <path d="m5 7.5 4.5 4.5L5 16.5M12 17h7" />,
  ask: <><circle cx="12" cy="12" r="8.5" /><path d="M9.6 9.7a2.5 2.5 0 0 1 4.8.9c0 1.6-2.4 2-2.4 3.6M12 17.2v.1" /></>,
  tool: PATHS.settings,
}

function Glyph({ name }) {
  return (
    <svg className="tool-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
         strokeLinejoin="round" aria-hidden="true">
      {GLYPHS[name] || GLYPHS.tool}
    </svg>
  )
}

// One tool call as [glyph, sentence]. Kept for callers that want a sentence;
// the rows now read icon · title · argument (turnEvents.toolLine).
export function humanizeTool(name, args = {}) {
  const l = toolLine(name, args)
  return [l.glyph, l.arg ? `${l.title} ${l.arg}` : l.title]
}

// Tool ids the reader has expanded. A finished turn collapses its live rows
// into a group, which remounts them; remembering the ids keeps a row the
// reader had open open, so finishing does not close what they were reading.
const openRows = new Set()
const rememberOpen = (id, on) => {
  if (id == null) return
  if (on) openRows.add(id)
  else openRows.delete(id)
  if (openRows.size > 400) openRows.clear()
}

// a running tool's elapsed time, ticking on its own so only this span redraws
function LiveMs({ t0 }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  if (t0 == null) return null
  const ms = now - t0
  return ms < 2000 ? null : <span className="tool-ms live">{fmtMs(ms)}</span>
}

const rowStatus = (part) => {
  if (!part.done) return 'live'
  if (part.interrupted) return 'stopped'
  return part.ok ? 'ok' : 'err'
}

// the expanded body, built only while open: args can be a whole file
function ToolDetail({ part, full, onFull }) {
  const args = clipText(JSON.stringify(part.args ?? {}, null, 2), 3000)
  const result = part.result != null ? clipText(part.result, full ? Infinity : 4000) : null
  return (
    <div className="tool-row-detail">
      <div className="dim">args</div>
      <pre className="tool-pre">{args.text}{args.hidden ? `\n… ${args.hidden} more characters` : ''}</pre>
      {result && <>
        <div className="dim">result</div>
        <pre className="tool-pre">{result.text}</pre>
        {result.hidden > 0 && (
          <button type="button" className="tool-more ghost" onClick={onFull}>
            show {result.hidden} more characters</button>
        )}
      </>}
      {part.result == null && !part.done && <div className="dim small">still running…</div>}
    </div>
  )
}

export const ToolRow = memo(function ToolRow({ part }) {
  const [open, setOpen] = useState(() => part.id != null && openRows.has(part.id))
  const [full, setFull] = useState(false)
  const status = rowStatus(part)
  const { glyph, title, arg } = toolLine(part.name, part.args)
  const exit = part.done && part.ok ? exitNote(part.result) : ''
  const badExit = exit && exit !== 'exit 0'
  const toggle = () => setOpen((o) => { rememberOpen(part.id, !o); return !o })
  return (
    <div className={`tool-row ${status}${badExit ? ' warn' : ''}${open ? ' open' : ''}`}>
      <div className="tool-row-head" role="button" tabIndex={0} aria-expanded={open}
           onClick={toggle}
           onKeyDown={(e) => {
             if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle() }
           }}>
        {status === 'live' && <span className="tool-spinner" />}
        {status === 'ok' && <span className="tool-ok">✓</span>}
        {status === 'err' && <span className="tool-fail">✕</span>}
        {status === 'stopped' && <span className="tool-stop" title="never reported back">–</span>}
        <Glyph name={glyph} />
        <span className="tool-title">{title}</span>
        {arg && <span className="tool-arg ellipsis" title={arg}>{arg}</span>}
        <span className="grow" />
        {exit && <span className={`tool-note${badExit ? ' bad' : ''}`}>{exit}</span>}
        {status === 'stopped' && <span className="tool-note">stopped</span>}
        {status === 'live' ? <LiveMs t0={part.t0} />
          : part.ms != null && <span className="tool-ms">{fmtMs(part.ms)}</span>}
        <span className={`chev ${open ? 'open' : ''}`} aria-hidden="true">›</span>
      </div>
      {status === 'err' && !open && (
        <div className="tool-err-line ellipsis" title={part.result}>{errorLine(part.result)}</div>
      )}
      {open && <ToolDetail part={part} full={full} onFull={() => setFull(true)} />}
    </div>
  )
})

// a multi-agent job a tool launched: its head, then the live tree
const JobRow = memo(function JobRow({ part }) {
  return (
    <div className="tool-row ok job-row">
      <div className="tool-row-head static"><Glyph name="tree" />
        <span className="tool-title">agents</span>
        <span className="tool-arg ellipsis" title={part.title}>{part.title}</span></div>
      <JobTree cid={part.root_id} />
    </div>
  )
})

// The finished, uneventful rows far behind the newest, folded to one line
// while the turn streams (turnEvents.foldParts); opens to the rows themselves.
const FoldRow = memo(function FoldRow({ parts }) {
  const [open, setOpen] = useState(false)
  const ms = parts.reduce((t, p) => t + (p.ms || 0), 0)
  const steps = parts.filter((p) => p.kind === 'tool').length
  return (
    <div className="tool-fold">
      <div className="tool-fold-head" role="button" tabIndex={0} aria-expanded={open}
           onClick={() => setOpen((o) => !o)}
           onKeyDown={(e) => {
             if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((o) => !o) }
           }}>
        <span className={`chev ${open ? 'open' : ''}`} aria-hidden="true">›</span>
        <span className="tool-ok">✓</span>
        <span>{steps} earlier step{steps === 1 ? '' : 's'}</span>
        {ms > 0 && <span className="tool-ms">{fmtMs(ms)}</span>}
      </div>
      {open && parts.map((p, i) => (p.kind === 'text'
        ? <Text key={`ft${i}`} text={p.text} />
        : <ToolRow key={p.id ?? i} part={p} />))}
    </div>
  )
})

// Finished turns collapse their activity into one header above the reply:
// how many steps, how long the turn took, and whether anything failed.
// `expanded` lets a transcript-wide "expand all" drive every group at once;
// each still toggles on its own afterwards.
export const ActivityGroup = memo(function ActivityGroup({ parts, expanded, ms }) {
  const [open, setOpen] = useState(() => !!expanded
    || parts.some((p) => p.id != null && openRows.has(p.id)))
  useEffect(() => { if (expanded !== undefined) setOpen(expanded) }, [expanded])
  if (!parts?.length) return null
  const tools = parts.filter((p) => p.kind === 'tool')
  const failed = tools.filter((p) => p.ok === false && !p.interrupted).length
  return (
    <div className="activity-group">
      <div className="steps-pill" role="button" tabIndex={0} aria-expanded={open}
           onClick={() => setOpen((o) => !o)}
           onKeyDown={(e) => {
             if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((o) => !o) }
           }}>
        <span className={`chev ${open ? 'open' : ''}`} aria-hidden="true">›</span>
        <span>{parts.length} step{parts.length !== 1 ? 's' : ''}</span>
        {ms != null && <span className="steps-dim">{fmtElapsed(ms)}</span>}
        {failed > 0 && <span className="steps-bad">{failed} failed</span>}
      </div>
      {open && parts.map((p, i) => (p.kind === 'job'
        ? <JobRow key={`job${p.root_id}`} part={p} />
        : <ToolRow key={p.id ?? i} part={p} />))}
    </div>
  )
})

function Typing() {
  return <span className="typing"><span /><span /><span /></span>
}

// Which brain wrote this. Only rendered when it was NOT one of the switcher's
// models (per /api/model) — voice runs a small model locally, and "who
// actually did this work" is not something you can tell from the prose.
export function ModelTag({ model }) {
  const m = useModel()
  if (!model || !m || isKnownModel(m, model)) return null
  return <span className="msg-model" title={`answered by ${model}`}>{model}</span>
}

// Markdown of one text run, parsed once per text: the transcript re-renders on
// every streamed chunk, and every finished message used to re-parse.
const Text = memo(function Text({ text }) { return <Md text={text} /> })

// The streaming message's parts as rows. Keys count texts and tools among
// themselves, so folding rows away never re-keys what stays.
function rowsOf(parts) {
  let texts = 0
  let tools = 0
  return foldParts(parts).map((p) => {
    if (p.kind === 'text') return <Text key={`t${texts++}`} text={p.text} />
    if (p.kind === 'fold') { tools += p.parts.length; return <FoldRow key={p.key} parts={p.parts} /> }
    if (p.kind === 'job') return <JobRow key={`job${p.root_id}`} part={p} />
    return <ToolRow key={p.id ?? `n${tools++}`} part={p} />
  })
}

export const MessageBody = memo(function MessageBody({ m }) {
  if (m.parts) {
    return (
      <div className="bubble">
        {m.parts.length === 0 && <Typing />}
        {rowsOf(m.parts)}
        <ModelTag model={m.model} />
      </div>
    )
  }
  if (!m.content && m.streaming) return <div className="bubble"><Typing /></div>
  return (
    <div className="bubble">
      {m.activity?.length > 0 && <ActivityGroup parts={m.activity} ms={m.ms} />}
      <Text text={m.content} />
      <ModelTag model={m.model} />
    </div>
  )
})

