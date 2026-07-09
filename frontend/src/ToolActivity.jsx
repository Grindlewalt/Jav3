import { useState } from 'react'
import JobTree from './JobTree.jsx'
import Md from './Md.jsx'

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

export function humanizeTool(name, args = {}) {
  switch (name) {
    case 'web_search': return `🔍 searching: ${trunc(args.query)}`
    case 'web_read': return `📄 reading ${host(args.url)}${args.extract ? ' (extracting)' : ''}`
    case 'read_and_summarize': {
      const urls = args.urls || (args.url ? [args.url] : [])
      return urls.length > 1 ? `📄 summarizing ${urls.length} pages`
        : `📄 summarizing ${host(urls[0] || '')}`
    }
    case 'research': return `🔬 researching: ${trunc(args.topic || args.query)}`
    case 'read_file': return `📄 reading ${args.path}${args.offset ? ` (line ${args.offset}+)` : ''}`
    case 'list_files': return '📂 listing files'
    case 'search_codebase': return `🔎 searching code: ${trunc(args.query)}`
    case 'crawl_codebase': return '🗺 indexing codebase'
    case 'write_file': return `✏️ staging ${args.path}`
    case 'edit_file': return `✏️ editing ${args.path}`
    case 'run_code': return '▶ running python'
    case 'run_command': return `▶ ${trunc(args.command)}`
    case 'run_gated': return `🛡 gated run: ${trunc(args.command, 50)}`
    case 'spawn_agent': return `🤖 ${args.agent}: ${trunc(args.task)}`
    case 'journal_update': return '📓 updating journal'
    case 'todo_update': return '☑ updating todos'
    case 'memory_write': return `🧠 remembering: ${args.name}`
    case 'memory_read': return args.name ? `🧠 recalling: ${args.name}` : '🧠 listing notes'
    case 'load_project': return `📁 loading project ${args.slug}`
    case 'git_status': return '🌿 git status'
    case 'git_diff': return '🌿 git diff'
    case 'git_commit_request': return `🌿 requesting commit: ${trunc(args.message, 50)}`
    case 'dashboard': return '📊 building dashboard'
    default: return `⚙ ${name}`
  }
}

export function ToolRow({ part }) {
  const [open, setOpen] = useState(false)
  const exit = part.done && /^exit (\d+)/.exec(part.result || '')
  const status = part.done ? (part.ok ? 'ok' : 'err') : 'live'
  return (
    <div className={`tool-row ${status}`}>
      <div className="tool-row-head" onClick={() => part.done && setOpen((o) => !o)}>
        <span className="grow ellipsis">
          {humanizeTool(part.name, part.args)}{exit ? ` — exit ${exit[1]}` : ''}
        </span>
        {part.done
          ? <span className="dim">{open ? '▾' : '▸'}</span>
          : <span className="tool-row-spin">···</span>}
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
      <div className="tool-row-head dim" onClick={() => setOpen((o) => !o)}>
        <span>{open ? '▾' : '▸'} {parts.length} step{parts.length !== 1 ? 's' : ''}</span>
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

export function MessageBody({ m }) {
  if (m.parts) {
    return (
      <div className="bubble">
        {m.parts.length === 0 && '…'}
        {m.parts.map((p, i) => {
          if (p.kind === 'text') return <Md key={i} text={p.text} />
          if (p.kind === 'job') return (
            <div key={`job${p.root_id}`} className="tool-row ok">
              <div className="tool-row-head"><span className="grow">🌳 {p.title}</span></div>
              <JobTree cid={p.root_id} />
            </div>
          )
          return <ToolRow key={p.id || i} part={p} />
        })}
      </div>
    )
  }
  return (
    <div className="bubble">
      {m.activity?.length > 0 && <ActivityGroup parts={m.activity} />}
      <Md text={m.content || (m.streaming ? '…' : '')} />
    </div>
  )
}
