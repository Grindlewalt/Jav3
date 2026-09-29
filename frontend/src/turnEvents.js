// Pure logic for a running turn's view: folding stream events into the
// transcript, one-line tool summaries, timing, the live "fold the older rows"
// grouping, and the follow-the-bottom arithmetic. No React and no DOM, so node
// can test it: frontend/src/__tests__/turnEvents.test.mjs.
//
// One implementation for every surface that shows a turn (the Work chat, a chat
// window, /shell): each used to carry its own copy of the event handler.

// ---- the transcript ----------------------------------------------------------

// The optimistic pair a send adds: the operator's line and the placeholder the
// turn streams into. `t0` is when this tab started watching, for the clock.
export function newTurn(text, now = Date.now()) {
  return [{ role: 'user', content: text },
          { role: 'assistant', content: '', streaming: true, parts: [], t0: now }]
}

// The message a turn is streaming into: the LAST one flagged `streaming`.
// Events used to land on "the last message", which is somebody else's the
// moment anything is appended after the placeholder.
export function streamingIndex(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) if (messages[i]?.streaming) return i
  return -1
}

// messages with fn applied to the streaming one; unchanged when none streams
// (a stale event for a turn this view has already settled or left)
export function patchStreaming(messages, fn) {
  const i = streamingIndex(messages)
  if (i === -1) return messages
  const copy = [...messages]
  copy[i] = fn(copy[i])
  return copy
}

// Fold one SSE event into the streaming assistant message. Pure: returns the
// updated message object. `now` is a parameter so tests can pin the clock.
export function applyTurnEvent(m, ev, now = Date.now()) {
  const parts = m.parts ? [...m.parts] : []
  if (ev.type === 'token') {
    const last = parts[parts.length - 1]
    if (last?.kind === 'text') parts[parts.length - 1] = { ...last, text: last.text + ev.text }
    else parts.push({ kind: 'text', text: ev.text })
  } else if (ev.type === 'tool') {
    parts.push({ kind: 'tool', id: ev.id, name: ev.name, args: ev.args, done: false, t0: now })
  } else if (ev.type === 'tool_result') {
    const i = parts.findIndex((p) => p.kind === 'tool' && !p.done
      && (ev.id ? p.id === ev.id : p.name === ev.name))
    if (i !== -1) {
      const t0 = parts[i].t0
      parts[i] = { ...parts[i], done: true, ok: ev.ok, result: ev.result,
                   ...(t0 != null ? { ms: Math.max(0, now - t0) } : {}) }
    } else if (ev.id && !parts.some((p) => p.kind === 'tool' && p.done
                                    && p.name === ev.name && p.result === ev.result)) {
      // joined mid-call: the call itself went out before this view attached, so
      // the result is all there is. It used to be dropped, and the row with it.
      parts.push({ kind: 'tool', id: ev.id, name: ev.name, args: {}, done: true,
                   ok: ev.ok, result: ev.result })
    }
  } else if (ev.type === 'job') {
    // a tool launched a multi-agent job — mount its live tree inline (once: a
    // re-announced job must not draw a second tree)
    if (!parts.some((p) => p.kind === 'job' && p.root_id === ev.root_id))
      parts.push({ kind: 'job', root_id: ev.root_id, title: ev.title })
  }
  return { ...m, parts }
}

// A tool that never reported back (the turn was stopped or died) is not a
// success: it keeps its row, marked interrupted.
const settle = (p) => (p.kind === 'job' || p.done ? p
  : { ...p, done: true, ok: false, interrupted: true })

// A finished message keeps only its tool rows (for the collapsed group), and
// how long the turn took.
export function finishTurn(m, content, now = Date.now()) {
  return {
    role: 'assistant', content,
    activity: (m.parts || []).filter((p) => p.kind === 'tool' || p.kind === 'job').map(settle),
    ...(m.t0 != null ? { ms: Math.max(0, now - m.t0) } : {}),
  }
}

// The turn died. Keep what it did (the tool rows, the text it had streamed)
// and put the error after it: an error used to REPLACE the message, taking
// every row of a long run with it.
export function failTurn(messages, message, now = Date.now()) {
  const i = streamingIndex(messages)
  const err = { role: 'error', content: message }
  if (i === -1) return [...messages, err]
  const m = messages[i]
  const parts = m.parts || []
  const copy = [...messages]
  if (!parts.length) { copy[i] = err; return copy }
  let lastTool = -1
  parts.forEach((p, j) => { if (p.kind !== 'text') lastTool = j })
  const tail = parts.slice(lastTool + 1).filter((p) => p.kind === 'text')
    .map((p) => p.text).join('')
  copy.splice(i, 1, finishTurn(m, tail, now), err)
  return copy
}

// Folds a turn's events into a message list via setMessages. Text tokens are
// batched (one state update per `delay` ms instead of one per token, each of
// which re-rendered the whole transcript); anything else flushes them first, so
// order is kept. `cancel` drops what is buffered (the view moved to another
// chat). Timer functions are parameters for the tests.
export function makeTurnFolder(setMessages, {
  delay = 40, now = Date.now, schedule = setTimeout, unschedule = clearTimeout,
} = {}) {
  let buf = ''
  let timer = null
  const flush = () => {
    if (timer != null) { unschedule(timer); timer = null }
    if (!buf) return
    const text = buf
    buf = ''
    const t = now()
    setMessages((m) => patchStreaming(m, (x) => applyTurnEvent(x, { type: 'token', text }, t)))
  }
  return {
    flush,
    cancel() {
      if (timer != null) unschedule(timer)
      timer = null
      buf = ''
    },
    handle(ev) {
      if (ev.type === 'token') {
        buf += ev.text || ''
        if (timer == null) timer = schedule(flush, delay)
        return
      }
      flush()
      const t = now()
      if (ev.type === 'tool' || ev.type === 'tool_result' || ev.type === 'job')
        setMessages((m) => patchStreaming(m, (x) => applyTurnEvent(x, ev, t)))
      else if (ev.type === 'final')
        setMessages((m) => patchStreaming(m, (x) => finishTurn(x, ev.content, t)))
      else if (ev.type === 'error')
        setMessages((m) => failTurn(m, ev.message, t))
    },
  }
}

// A turn's activity, for the status line: how many tool calls, how many still
// running, how many failed (an interrupted call is not a failure).
export function turnStats(parts = []) {
  let tools = 0
  let running = 0
  let failed = 0
  let current = null
  for (const p of parts) {
    if (p.kind !== 'tool') continue
    tools += 1
    if (!p.done) { running += 1; current = p }
    else if (p.ok === false && !p.interrupted) failed += 1
  }
  return { tools, running, failed, current }
}

// A number that grows whenever something new shows up in the transcript: a
// message, a tool row, a job, a new text run. The "N new" pill is the growth
// since the reader scrolled away. (Tokens inside a text run do not count.)
export function activityMark(messages = []) {
  let n = messages.length
  const last = messages[messages.length - 1]
  if (last?.streaming) n += (last.parts || []).length
  return n
}

// ---- one-line summaries ------------------------------------------------------

export function host(u) {
  try { return new URL(u).host } catch { return String(u ?? '') }
}

export function trunc(s, n = 60) {
  s = String(s ?? '')
  return s.length > n ? `${s.slice(0, n)}…` : s
}

const oneLine = (s, n = 80) => trunc(String(s ?? '').trim().split('\n')[0], n)

// keys that usually carry the point of a call, in the order to look
const KEY_ARGS = ['path', 'file', 'url', 'query', 'command', 'code', 'name', 'title', 'message',
  'text', 'slug', 'topic', 'task', 'id', 'action']

// The most telling argument of an unknown tool, on one line
export function keyArg(args) {
  if (!args || typeof args !== 'object') return ''
  for (const k of KEY_ARGS) {
    const v = args[k]
    if (typeof v === 'string' && v.trim()) return oneLine(v)
    if (typeof v === 'number') return String(v)
  }
  for (const v of Object.values(args)) if (typeof v === 'string' && v.trim()) return oneLine(v)
  return ''
}

// One tool call as { glyph, title, arg }: the row reads icon · title · arg.
// Glyph names index ToolActivity's GLYPHS. `title` is a short verb or the tool's
// own name; `arg` is the one argument worth reading without opening the row.
export function toolLine(name, args = {}) {
  const a = args || {}
  switch (name) {
    case 'web_search': return { glyph: 'search', title: 'search', arg: oneLine(a.query) }
    case 'web_read': return { glyph: 'page', title: 'read',
      arg: `${host(a.url)}${a.extract ? ' (extracting)' : ''}` }
    case 'read_and_summarize': {
      const urls = a.urls || (a.url ? [a.url] : [])
      return { glyph: 'page', title: a.triage ? 'triage' : 'summarize',
        arg: urls.length > 1 ? `${urls.length} pages` : host(urls[0] || '') }
    }
    case 'research': return { glyph: 'research', title: 'research', arg: oneLine(a.topic || a.query) }
    case 'read_file': return { glyph: 'page', title: 'read',
      arg: `${a.path ?? ''}${a.offset ? ` (line ${a.offset}+)` : ''}` }
    case 'list_files': return { glyph: 'folder', title: 'list files', arg: a.path || '' }
    case 'search_codebase': return { glyph: 'search', title: 'search code', arg: oneLine(a.query) }
    case 'crawl_codebase': return { glyph: 'map', title: 'index code', arg: '' }
    case 'write_file': return { glyph: 'write', title: 'write', arg: a.path ?? '' }
    case 'edit_file': return { glyph: 'write', title: 'edit', arg: a.path ?? '' }
    case 'run_code': return { glyph: 'run', title: 'run', arg: oneLine(a.command || a.code) }
    case 'spawn_agent': return { glyph: 'agent', title: a.agent || 'agent', arg: oneLine(a.task) }
    case 'deploy_agents': return { glyph: 'tree', title: 'deploy agents',
      arg: oneLine(a.title || a.brief) }
    case 'create_agent': return { glyph: 'agent', title: 'create agent', arg: oneLine(a.name, 40) }
    case 'schedule_update': return { glyph: 'schedule', title: `schedule ${a.action || 'update'}`,
      arg: oneLine(a.name, 40) }
    case 'journal_update': return { glyph: 'book', title: 'journal', arg: '' }
    case 'todo_update': return { glyph: 'todo', title: 'todos', arg: '' }
    case 'memory_write': return { glyph: 'book', title: 'remember', arg: a.name ?? '' }
    case 'memory_read': return { glyph: 'book', title: a.name ? 'recall' : 'list notes', arg: a.name ?? '' }
    case 'load_project': return { glyph: 'folder', title: 'load project', arg: a.slug ?? '' }
    case 'git_status': return { glyph: 'branch', title: 'git status', arg: '' }
    case 'git_diff': return { glyph: 'branch', title: 'git diff', arg: '' }
    case 'git_commit_request': return { glyph: 'branch', title: 'commit request', arg: oneLine(a.message, 50) }
    case 'git_push_request': return { glyph: 'branch', title: 'review request', arg: oneLine(a.title, 50) }
    case 'dashboard': return { glyph: 'chart', title: 'dashboard', arg: '' }
    case 'ask_user': {
      const q = Array.isArray(a.questions) ? a.questions[0] : null
      return { glyph: 'ask', title: 'ask you', arg: oneLine(q?.question || a.question) }
    }
    default: return { glyph: 'tool', title: name, arg: keyArg(a) }
  }
}

// "exit 0" and friends: how a run ended, when the result says so
export function exitNote(result) {
  const m = /^exit (-?\d+)/.exec(result || '')
  return m ? `exit ${m[1]}` : ''
}

// The first line of a failed call's result, for the row: what went wrong,
// without opening it
export function errorLine(result, n = 140) {
  const s = String(result ?? '').replace(/^error:\s*/i, '').trim()
  return s ? oneLine(s, n) : 'failed'
}

// ---- time and money ----------------------------------------------------------

// a tool's duration on its row: 0.4s, 12s, 1m 05s
export function fmtMs(ms) {
  if (ms == null || Number.isNaN(ms)) return ''
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return `${m}m ${String(Math.round(s - m * 60)).padStart(2, '0')}s`
}

// the turn clock: 45s, 2m 13s, 1h 04m
export function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor((ms || 0) / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`
}

export function fmtCost(usd) {
  if (usd == null || Number.isNaN(usd)) return ''
  if (usd <= 0) return '$0.00'
  if (usd < 0.01) return '<$0.01'
  return `$${usd.toFixed(2)}`
}

// ---- clipping ----------------------------------------------------------------

// Clip a long result: what to draw, and how much was left out. A tool can
// return 10k characters; drawing all of it in a row that started collapsed
// made the row's opening a full-screen event.
export function clipText(s, max = 4000) {
  const text = String(s ?? '')
  if (text.length <= max) return { text, hidden: 0 }
  return { text: text.slice(0, max), hidden: text.length - max }
}

// ---- the live view: fold the older rows ----------------------------------------

// A long run is a wall of rows. While it streams, the finished, uneventful
// ones far behind the newest fold into a single "N earlier steps" line, so the
// screen holds what is happening now. Errors, running calls, jobs and text
// never fold. Returns display items: the part itself, or
// { kind: 'fold', key, parts } standing for a run of them.
export function foldParts(parts = [], { keep = 6, min = 3 } = {}) {
  // how many tool rows sit after each index
  const after = new Array(parts.length).fill(0)
  let seen = 0
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    after[i] = seen
    if (parts[i].kind === 'tool') seen += 1
  }
  const foldable = (p, i) => p.kind === 'tool' && p.done && p.ok !== false && after[i] >= keep
  const out = []
  let run = []
  const close = () => {
    if (run.length >= min) out.push({ kind: 'fold', key: `fold:${run[0].id ?? run[0].t0 ?? out.length}`, parts: run })
    else out.push(...run)
    run = []
  }
  parts.forEach((p, i) => {
    if (foldable(p, i)) run.push(p)
    else { close(); out.push(p) }
  })
  close()
  return out
}

// ---- following the bottom ------------------------------------------------------

// Is the reader at (or within `slack` px of) the end of a scroller?
export function atBottom(scrollTop, clientHeight, scrollHeight, slack = 48) {
  return scrollHeight - (scrollTop + clientHeight) <= slack
}
