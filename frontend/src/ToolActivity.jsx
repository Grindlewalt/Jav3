import { useState } from 'react'
import JobTree from './JobTree.jsx'
import Md from './Md.jsx'
import { useModel } from './modelInfo.js'
import { PATHS } from './nav.jsx'

// Live tool-activity rendering shared by Chat and ChatBox: humanized one-line
// rows that update as results land, with click-to-expand args/result.
// SECURITY: tool args and results are UNTRUSTED text (they can contain web
// content) — they only ever render inside <pre>, never through <Md>.

function host(u) {
  try { return new URL(u).host } catch { return u }
}

function trunc(s, n = 60) {
  s = String(s ?? '')
  return s.length > n ? s.slice(0, n) + '…' : s
}

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

// One tool call as [glyph, sentence].
export function humanizeTool(name, args = {}) {
  switch (name) {
    case 'web_search': return ['search', `searching: ${trunc(args.query)}`]
    case 'web_read': return ['page', `reading ${host(args.url)}${args.extract ? ' (extracting)' : ''}`]
    case 'read_and_summarize': {
      const urls = args.urls || (args.url ? [args.url] : [])
      const mode = args.triage ? ' (triage)' : ''
      return ['page', urls.length > 1 ? `summarizing ${urls.length} pages${mode}`
        : `summarizing ${host(urls[0] || '')}${mode}`]
    }
    case 'research': return ['research', `researching: ${trunc(args.topic || args.query)}`]
    case 'read_file': return ['page', `reading ${args.path}${args.offset ? ` (line ${args.offset}+)` : ''}`]
    case 'list_files': return ['folder', 'listing files']
    case 'search_codebase': return ['search', `searching code: ${trunc(args.query)}`]
    case 'crawl_codebase': return ['map', 'indexing codebase']
    case 'write_file': return ['write', `writing ${args.path}`]
    case 'edit_file': return ['write', `editing ${args.path}`]
    case 'spawn_agent': return ['agent', `${args.agent}: ${trunc(args.task)}`]
    case 'deploy_agents': return ['tree', `deploying agents: ${trunc(args.title || args.brief)}`]
    case 'create_agent': return ['agent', `creating agent: ${trunc(args.name, 40)}`]
    case 'schedule_update':
      return ['schedule', `schedule ${args.action || 'update'}${args.name ? `: ${trunc(args.name, 40)}` : ''}`]
    case 'journal_update': return ['book', 'updating journal']
    case 'todo_update': return ['todo', 'updating todos']
    case 'memory_write': return ['book', `remembering: ${args.name}`]
    case 'memory_read': return ['book', args.name ? `recalling: ${args.name}` : 'listing notes']
    case 'load_project': return ['folder', `loading project ${args.slug}`]
    case 'git_status': return ['branch', 'git status']
    case 'git_diff': return ['branch', 'git diff']
    case 'git_commit_request': return ['branch', `requesting commit: ${trunc(args.message, 50)}`]
    case 'dashboard': return ['chart', 'building dashboard']
    default: return ['tool', name]
  }
}

export function ToolRow({ part }) {
  const [open, setOpen] = useState(false)
  const exit = part.done && /^exit (\d+)/.exec(part.result || '')
  const status = part.done ? (part.ok ? 'ok' : 'err') : 'live'
  const [glyph, label] = humanizeTool(part.name, part.args)
  return (
    <div className={`tool-row ${status}`}>
      <div className="tool-row-head" onClick={() => part.done && setOpen((o) => !o)}>
        {!part.done && <span className="tool-spinner" />}
        {part.done && (part.ok
          ? <span className="tool-ok">✓</span>
          : <span className="tool-fail">✕</span>)}
        <Glyph name={glyph} />
        <span className="grow ellipsis">
          {label}{exit ? ` — exit ${exit[1]}` : ''}
        </span>
        {part.done && <span className={`chev ${open ? 'open' : ''}`} aria-hidden="true">›</span>}
      </div>
      {open && (
        <div className="tool-row-detail">
          <div className="dim">args</div>
          <pre className="tool-pre">{JSON.stringify(part.args ?? {}, null, 2)}</pre>
          {part.result != null && <>
            <div className="dim">result</div>
            <pre className="tool-pre">{part.result}</pre>
          </>}
        </div>
      )}
    </div>
  )
}

// Finished turns collapse their activity into one header above the reply.
export function ActivityGroup({ parts }) {
  const [open, setOpen] = useState(false)
  if (!parts?.length) return null
  return (
    <div className="activity-group">
      <div className="steps-pill" onClick={() => setOpen((o) => !o)}>
        <span className={`chev ${open ? 'open' : ''}`} aria-hidden="true">›</span>
        <span>{parts.length} step{parts.length !== 1 ? 's' : ''}</span>
      </div>
      {open && parts.map((p, i) => p.kind === 'job'
        ? <div key={`job${p.root_id}`} className="tool-row ok"><JobTree cid={p.root_id} /></div>
        : <ToolRow key={p.id || i} part={p} />)}
    </div>
  )
}

// Fold one SSE event into the streaming assistant message. Pure — returns the
// updated message object.
export function applyTurnEvent(m, ev) {
  const parts = m.parts ? [...m.parts] : []
  if (ev.type === 'token') {
    const last = parts[parts.length - 1]
    if (last?.kind === 'text') parts[parts.length - 1] = { ...last, text: last.text + ev.text }
    else parts.push({ kind: 'text', text: ev.text })
  } else if (ev.type === 'tool') {
    parts.push({ kind: 'tool', id: ev.id, name: ev.name, args: ev.args, done: false })
  } else if (ev.type === 'tool_result') {
    const i = parts.findIndex((p) => p.kind === 'tool' && !p.done
      && (ev.id ? p.id === ev.id : p.name === ev.name))
    if (i !== -1) parts[i] = { ...parts[i], done: true, ok: ev.ok, result: ev.result }
  } else if (ev.type === 'job') {
    // a tool launched a multi-agent job — mount its live tree inline
    parts.push({ kind: 'job', root_id: ev.root_id, title: ev.title })
  }
  return { ...m, parts }
}

// A finished message keeps only its tool rows (for the collapsed group).
export function finishTurn(m, content) {
  return {
    role: 'assistant', content,
    activity: (m.parts || []).filter((p) => p.kind === 'tool' || p.kind === 'job')
      .map((p) => (p.kind === 'job' || p.done ? p : { ...p, done: true, ok: true })),
  }
}

function Typing() {
  return <span className="typing"><span /><span /><span /></span>
}

// Which brain wrote this. Only rendered when it was NOT one of the switcher's
// models (per /api/model) — voice runs a small model locally, and "who
// actually did this work" is not something you can tell from the prose.
function ModelTag({ model }) {
  const m = useModel()
  if (!model || !m || model === m.default || m.choices.includes(model)) return null
  return <span className="msg-model" title={`answered by ${model}`}>{model}</span>
}

export function MessageBody({ m }) {
  if (m.parts) {
    return (
      <div className="bubble">
        {m.parts.length === 0 && <Typing />}
        {m.parts.map((p, i) => {
          if (p.kind === 'text') return <Md key={i} text={p.text} />
          if (p.kind === 'job') return (
            <div key={`job${p.root_id}`} className="tool-row ok">
              <div className="tool-row-head"><Glyph name="tree" />
                <span className="grow ellipsis">{p.title}</span></div>
              <JobTree cid={p.root_id} />
            </div>
          )
          return <ToolRow key={p.id || i} part={p} />
        })}
        <ModelTag model={m.model} />
      </div>
    )
  }
  if (!m.content && m.streaming) return <div className="bubble"><Typing /></div>
  return (
    <div className="bubble">
      {m.activity?.length > 0 && <ActivityGroup parts={m.activity} />}
      <Md text={m.content} />
      <ModelTag model={m.model} />
    </div>
  )
}
