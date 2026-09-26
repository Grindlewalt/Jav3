import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { WorkContext } from '../work/context.js'
import { COMMANDS } from './commands.js'
import { completion, filterOptions, findCommand, matchCommands, parseInput } from './parse.js'
import './slash.css'

// Slash commands and mid-turn messages for the Chat composer.
//
//   const slash = useSlash(host)
//   <textarea onKeyDown={(e) => { if (slash.onKeyDown(e)) return; ... }} />
//   <form onSubmit={(e) => { e.preventDefault(); slash.submit() || send() }}>
//   {slash.popup}                       // above the composer's input row
//   handleTurnEvent(ev) { slash.onEvent(ev); ... }
//   POST body for a NEW chat: ...slash.newChatFields   ({agent} / {mode})
//
// host (from Chat.jsx, re-read every render — a stale closure is never used):
//   input, setInput, busy, conversationId, turnId(), conversations, projects,
//   active, pendingProject, messages, setMessages, temporary, setTemporary,
//   send(text), newChat(), openChat(id), openList(), pickProject(mode, slug),
//   stop(), rename(), deleteChat(), refresh(), refreshProjects()
//
// Mid-turn: while a turn streams, Enter POSTs /api/chat/{id}/message. The
// message shows as "queued" until the stream's operator_message, then
// "delivered". 409 (the turn just ended) / 404 / 405 send it as the next
// turn; `final.undelivered` does the same (after a stop they go back to the
// composer instead, like the terminal).
export function useSlash(host) {
  const navigate = useNavigate()
  const work = useContext(WorkContext)
  const hostRef = useRef(host)
  hostRef.current = host
  const workRef = useRef(work)
  workRef.current = work

  const [sel, setSel] = useState(0)
  const [dismissed, setDismissed] = useState(null)   // the input Esc closed the popup at
  const [note, setNote] = useState(null)             // {text, kind, at}
  const [opts, setOpts] = useState({})               // command name -> options | 'loading' | Error
  const [nextChat, setNextChat] = useState(null)     // {agent?, mode?, label} for the next NEW chat
  const [later, setLater] = useState([])             // texts to send once no turn runs
  const [dump, setDump] = useState(null)             // {project, text}: /orchestration's box
  const [, setTick] = useState(0)
  const after = useRef([])
  const seq = useRef(0)
  const stopAsked = useRef(false)
  const listRef = useRef(null)

  const input = host.input
  const p = parseInput(input)
  const cmdMode = !!p && !p.escaped
  let rows = []
  let argCmd = null
  if (cmdMode && !p.spaced) {
    rows = matchCommands(COMMANDS, p.name).map((r) => ({ kind: 'cmd', ...r }))
  } else if (cmdMode) {
    argCmd = findCommand(COMMANDS, p.name)
    const o = argCmd && opts[argCmd.name]
    if (Array.isArray(o)) rows = filterOptions(o, p.arg).map((x) => ({ kind: 'option', ...x }))
  }
  const open = cmdMode && dismissed !== input
  const cur = rows.length ? Math.min(sel, rows.length - 1) : -1

  const say = useCallback((text, kind = 'info', at = '') => setNote({ text, kind, at }), [])
  const queueLater = useCallback((t) => setLater((l) => [...l, t]), [])

  // --- effects -----------------------------------------------------------------

  // a new query starts at the top
  useEffect(() => { setSel(0) }, [input])

  // the argument's options: fetched when the popup reaches the argument,
  // dropped when it closes so the next opening is fresh
  const argName = argCmd?.args ? argCmd.name : null
  useEffect(() => {
    if (!argName || opts[argName] !== undefined) return
    setOpts((o) => ({ ...o, [argName]: 'loading' }))
    Promise.resolve()
      .then(() => findCommand(COMMANDS, argName).args(envRef.current))
      .then((list) => setOpts((o) => ({ ...o, [argName]: list || [] })))
      .catch((err) => setOpts((o) => ({ ...o, [argName]: new Error(err.detail || err.message) })))
  }, [argName])   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!cmdMode && Object.keys(opts).length) setOpts({})
  }, [cmdMode])   // eslint-disable-line react-hooks/exhaustive-deps

  // work a command queued for after the render its state change causes
  useEffect(() => {
    if (!after.current.length) return
    const q = after.current
    after.current = []
    for (const fn of q) fn(hostRef.current)
  })

  // one queued text at a time, whenever no turn is running
  useEffect(() => {
    if (host.busy || !later.length) return
    const [t, ...rest] = later
    setLater(rest)
    hostRef.current.send(t)
  }, [host.busy, later])

  // an identity binds at creation: once the chat exists it's spent
  useEffect(() => { if (host.conversationId) setNextChat(null) }, [host.conversationId])

  useEffect(() => {
    listRef.current?.querySelector('.slash-row.sel')?.scrollIntoView({ block: 'nearest' })
  }, [cur, open])

  // --- commands ----------------------------------------------------------------

  const envRef = useRef(null)
  envRef.current = {
    get host() { return hostRef.current },
    get work() { return workRef.current },
    navigate,
    say,
    later: queueLater,
    setNextChat,
    afterRender: (fn) => { after.current.push(fn); setTick((t) => t + 1) },
    openDump: (project) => setDump({ project, text: '' }),
    markStop: () => { stopAsked.current = true },
  }

  async function run(cmd, arg) {
    const h = hostRef.current
    if (h.busy && !cmd.busyOk) {
      say(`/${cmd.name} waits for this turn to finish — /stop ends it`, 'warn', h.input)
      return
    }
    h.setInput('')
    setDismissed(null)
    setNote(null)
    try {
      const msg = await cmd.run(arg || '', envRef.current)
      // /help re-opens the list, so its note belongs to the "/" it leaves
      if (msg) say(msg, 'info', cmd.name === 'help' ? '/' : '')
    } catch (err) {
      say(err.detail || err.message || String(err), 'error')
    }
  }

  function accept(row) {
    const h = hostRef.current
    if (row.kind === 'cmd') {
      if (row.cmd.args || row.cmd.usage) h.setInput(completion(row))
      else run(row.cmd, '')
    } else if (String(row.value).endsWith(' ')) {
      h.setInput(completion(row, argCmd))   // "create " wants more typing
    } else {
      run(argCmd, String(row.value))
    }
  }

  // --- mid-turn messages ---------------------------------------------------------

  const patchMid = (key, fn) => hostRef.current.setMessages((m) =>
    m.flatMap((x) => (x.mtKey === key ? fn(x) : [x])))

  async function midturn(text) {
    const h = hostRef.current
    const id = h.turnId()
    if (!id) {
      // the turn has no id yet (its stream hasn't started): after it
      queueLater(text)
      say('queued — it goes as the next message when this turn ends')
      return
    }
    const key = ++seq.current
    h.setMessages((m) => {
      const c = [...m]
      const at = c[c.length - 1]?.streaming ? c.length - 1 : c.length
      c.splice(at, 0, { role: 'user', content: text, midturn: 'queued', mtKey: key })
      return c
    })
    try {
      await api(`/api/chat/${id}/message`, { method: 'POST', body: JSON.stringify({ text }) })
    } catch (err) {
      if ([404, 405, 409].includes(err.status)) {
        // no_turn_running: it just ended (or an older server) — next turn
        patchMid(key, () => [])
        queueLater(text)
      } else {
        patchMid(key, (x) => [{ ...x, midturn: 'failed' }])
        say(`not sent — ${err.detail || err.message}`, 'error')
      }
    }
  }

  const onEvent = useCallback((ev) => {
    const h = hostRef.current
    if (ev.type === 'start') stopAsked.current = false
    if (ev.type === 'error')
      h.setMessages((m) => m.map((x) => (x.midturn === 'queued' ? { ...x, midturn: 'failed' } : x)))
    if (ev.type === 'operator_message') {
      const t = String(ev.text || '').trim()
      h.setMessages((m) => {
        const i = m.findIndex((x) => x.midturn === 'queued' && x.content.trim() === t)
        const c = [...m]
        if (i >= 0) c[i] = { ...c[i], midturn: 'delivered' }
        else {
          // sent from somewhere else (another tab, the terminal)
          const at = c[c.length - 1]?.streaming ? c.length - 1 : c.length
          c.splice(at, 0, { role: 'user', content: ev.text, midturn: 'delivered' })
        }
        return c
      })
    }
    if (ev.type === 'final' && ev.undelivered?.length) {
      const texts = ev.undelivered.map(String)
      const gone = new Set(texts.map((t) => t.trim()))
      h.setMessages((m) => m.filter((x) =>
        !(x.midturn === 'queued' && gone.has(x.content.trim()))))
      if (stopAsked.current || String(ev.content || '').includes('[Request interrupted')) {
        // stopped: don't start a turn the operator just ended — back to the bar
        h.setInput((cur) => [...texts, cur].filter(Boolean).join('\n'))
      } else {
        setLater((l) => [...l, ...texts])
      }
    }
  }, [])

  // --- keys ----------------------------------------------------------------------

  // Enter (or the send button). true = handled here, don't send.
  function submit() {
    const h = hostRef.current
    const text = h.input
    const q = parseInput(text)
    if (q?.escaped) {
      if (!q.text.trim()) return true
      h.setInput('')
      if (h.busy) midturn(q.text.trim())
      else h.send(q.text)
      return true
    }
    if (q) {
      const cmd = findCommand(COMMANDS, q.name)
      if (!cmd) {
        say(q.name
          ? `unknown command /${q.name} — / lists them; //${q.name} sends it as a message`
          : 'type a command — / lists them', 'warn', text)
        return true
      }
      run(cmd, q.arg)
      return true
    }
    if (h.busy) {
      if (text.trim()) { h.setInput(''); midturn(text.trim()) }
      return true
    }
    return false
  }

  function onKeyDown(e) {
    if (e.nativeEvent?.isComposing) return false
    const plainEnter = e.key === 'Enter' && !e.shiftKey && !e.altKey
    if (open && rows.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const d = e.key === 'ArrowDown' ? 1 : -1
        setSel((cur + d + rows.length) % rows.length)
        return true
      }
      if (e.key === 'Tab') {
        e.preventDefault()
        hostRef.current.setInput(completion(rows[cur], argCmd))
        return true
      }
      if (plainEnter) {
        e.preventDefault()
        accept(rows[cur])
        return true
      }
    }
    if (open && e.key === 'Escape') {
      e.preventDefault()
      setDismissed(input)
      return true
    }
    if (plainEnter && submit()) {
      e.preventDefault()
      return true
    }
    return false
  }

  // --- the element ---------------------------------------------------------------

  function startDump() {
    const text = dump.text.trim()
    if (!text) return
    const project = dump.project
    setDump(null)
    const h = hostRef.current
    if (h.busy) { say('a turn is running — /stop it or wait, then /orchestration', 'warn'); return }
    h.newChat()
    setNextChat({ mode: 'orchestrate', label: `orchestrator · ${project}` })
    // after the new chat has rendered, so the pin lands on it and the send
    // carries it (not on the chat that was open)
    envRef.current.afterRender((fresh) => {
      fresh.pickProject('pin', project)
      queueLater(text)
    })
  }

  const argState = argCmd?.args ? opts[argCmd.name] : undefined
  const showNote = note && note.at === input && !dump
  let popup = null
  if (dump) {
    popup = (
      <div className="slash-pop slash-dump" role="dialog" aria-label="orchestration brain-dump">
        <div className="slash-head">
          <span className="slash-name">/orchestration</span>
          <span className="slash-help">project <b>{dump.project}</b></span>
        </div>
        <textarea
          autoFocus rows={7} value={dump.text}
          onChange={(e) => setDump((d) => ({ ...d, text: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === 'Escape') { e.preventDefault(); setDump(null) }
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startDump() }
          }}
          placeholder={'Dump the problem: goals, what’s broken, what you know, what done '
            + 'looks like. The orchestrator splits it into tasks, sends an agent to each '
            + 'and messages them as they work.\n\nEvery agent uses the default model unless '
            + 'you name one for a task.'} />
        <div className="slash-actions">
          <span className="slash-keys">⌘/Ctrl+Enter starts · Esc cancels</span>
          <button type="button" className="ghost" onClick={() => setDump(null)}>Cancel</button>
          <button type="button" disabled={!dump.text.trim()} onClick={startDump}>
            Start orchestration</button>
        </div>
      </div>
    )
  } else if (open || showNote || nextChat) {
    const head = argCmd
      ? <><span className="slash-name">/{argCmd.name}</span>
          {argCmd.usage && <span className="slash-usage">{argCmd.usage}</span>}
          <span className="slash-help">{argCmd.help}</span></>
      : null
    popup = (
      <div className="slash-pop" onMouseDown={(e) => e.preventDefault()}>
        {nextChat && (
          <div className="slash-next">
            <span className="grow">Next new chat runs as <b>{nextChat.label}</b></span>
            <button type="button" className="ghost" title="clear"
                    onClick={() => setNextChat(null)}>✕</button>
          </div>
        )}
        {open && head && <div className="slash-head">{head}</div>}
        {open && (rows.length > 0 ? (
          <div className="slash-list" role="listbox" ref={listRef}>
            {rows.map((r, i) => (
              <button key={r.kind === 'cmd' ? r.cmd.name : `o${r.value}`}
                      type="button" role="option" aria-selected={i === cur}
                      className={`slash-row${i === cur ? ' sel' : ''}`}
                      onMouseEnter={() => setSel(i)} onClick={() => accept(r)}>
                {r.kind === 'cmd' ? <>
                  <span className="slash-name">/{r.cmd.name}</span>
                  {r.cmd.usage && <span className="slash-usage">{r.cmd.usage}</span>}
                  <span className="slash-help">{r.via ? `/${r.via} · ` : ''}{r.cmd.help}</span>
                </> : <>
                  <span className="slash-name">{r.label || r.value}</span>
                  {r.meta && <span className="slash-help">{r.meta}</span>}
                </>}
              </button>
            ))}
          </div>
        ) : argState === 'loading' ? <div className="slash-empty">loading…</div>
          : argState instanceof Error ? <div className="slash-empty">couldn’t load: {argState.message}</div>
          : !p.spaced ? <div className="slash-empty">no command starts with /{p.name} — Enter
              says so, //{p.name} sends it as a message</div>
          : !argCmd ? <div className="slash-empty">unknown command /{p.name}</div>
          : argCmd.args ? <div className="slash-empty">no match — Enter uses “{p.arg}”</div>
          : <div className="slash-empty">Enter runs /{argCmd.name}{p.arg ? ` ${p.arg}` : ''}</div>)}
        {showNote && <div className={`slash-note ${note.kind}`}>{note.text}</div>}
        {open && (
          <div className="slash-keys">↑↓ move · Tab complete · Enter run · Esc close
            {' · '}//text sends a slash</div>
        )}
      </div>
    )
  }

  return {
    onKeyDown,
    submit,
    onEvent,
    popup,
    newChatFields: nextChat
      ? { ...(nextChat.agent ? { agent: nextChat.agent } : {}),
          ...(nextChat.mode ? { mode: nextChat.mode } : {}) }
      : {},
  }
}
