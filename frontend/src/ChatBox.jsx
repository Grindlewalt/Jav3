import { useEffect, useRef, useState } from 'react'
import { api, chatStream, tailStream } from './api.js'
import { applyTurnEvent, finishTurn, MessageBody } from './ToolActivity.jsx'
import { useAsk } from './ask.jsx'
import { AskPanel, PermissionModeSelect, useOperatorAsks, usePermissionMode } from './AskUser.jsx'
import Button from './components/Button.jsx'
import Menu, { MenuItem, MenuSep } from './components/Menu.jsx'
import Tag from './components/Tag.jsx'
import EmptyState from './components/EmptyState.jsx'

// Compact chat, embeddable anywhere (board panel). When projectSlug is set,
// conversations are filtered to that project and new ones are linked to it.
// `initialId` opens that conversation on mount ('new': a fresh chat) instead
// of the running / latest one; `onOpened(id | 'new')` reports each switch, so
// a Work window can remember which conversation it holds.
//
// A thread can run AS an agent (its AGENT.md prompt leads, its skills are its
// own) — an agent thread is still a chat, so it keeps history, compaction,
// detach/re-attach and stop. Identity binds at creation, like the project pin:
// the "+ new" menu picks who the NEXT thread runs as, and an open thread shows
// the identity it was created with.
export default function ChatBox({ projectSlug, initialId, onOpened }) {
  const [convos, setConvos] = useState([])
  const [cid, setCid] = useState(null)
  const [agents, setAgents] = useState([])
  const [newAs, setNewAs] = useState('')          // '' = Jav3; for a thread not yet sent
  const [threadAs, setThreadAs] = useState('')    // the open thread's agent_slug
  const [newMenu, setNewMenu] = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const bottomRef = useRef(null)
  const tailAbort = useRef(null)   // cancels a resume-tail on switch/unmount
  const ask = useAsk()
  const asks = useOperatorAsks(cid)          // ask_user / permission asks
  const [permMode, setPermMode] = usePermissionMode(cid)

  useEffect(() => () => tailAbort.current?.abort(), [])
  const reported = useRef(false)
  useEffect(() => {
    // not the mount's null: that is "not open yet", not the operator's choice
    if (!reported.current) { reported.current = true; if (!cid) return }
    onOpened?.(cid ?? 'new')
  }, [cid]) // eslint-disable-line
  // the roster for the picker; failure just leaves it Jav3-only
  useEffect(() => {
    api('/api/agents').then((r) => setAgents(r.agents)).catch(() => {})
  }, [])

  // shared by the live POST stream and a resumed background-turn tail
  function handleTurnEvent(ev) {
    asks.onEvent(ev)
    if (['token', 'tool', 'tool_result', 'job'].includes(ev.type))
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = applyTurnEvent(copy[copy.length - 1], ev)
        return copy
      })
    if (ev.type === 'final')
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = finishTurn(copy[copy.length - 1], ev.content)
        return copy
      })
    if (ev.type === 'error')
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = { role: 'error', content: ev.message }
        return copy
      })
  }

  const refresh = () =>
    api(`/api/conversations${projectSlug ? `?project=${encodeURIComponent(projectSlug)}` : ''}`)
      .then((r) => { setConvos(r.conversations); return r.conversations })
  useEffect(() => {
    // a board panel remounts on every open: resume where the operator left
    // off — a running turn always wins, else the project's latest chat —
    // instead of amnesia into "new chat" while work continues server-side
    refresh().then((list) => {
      if (initialId === 'new') return
      if (initialId && list.some((c) => c.id === initialId)) { open(initialId); return }
      const running = list.find((c) => c.running)

      const target = running || (projectSlug ? list[0] : null)
      if (target) open(target.id)
    }).catch(() => {})
  }, [projectSlug]) // eslint-disable-line

  async function pick(id) {
    setShowHistory(false)
    await open(id)
  }

  function newChat(as = '') {
    setShowHistory(false)
    setNewMenu(false)
    setNewAs(as)
    setThreadAs('')
    setCid(null)
    setMessages([])
  }

  async function del(id, e) {
    e.stopPropagation()
    if (!await ask.confirm(`Delete chat #${id}?`,
                           { confirmLabel: 'Delete', danger: true })) return
    await api(`/api/conversations/${id}`, { method: 'DELETE' })
    if (id === cid) newChat()
    refresh()
  }

  useEffect(() => {
    // scroll ONLY the message list — scrollIntoView walks every scrollable
    // ancestor and yanked the whole workspace board to the bottom on stream
    const box = bottomRef.current?.parentElement
    if (box) box.scrollTop = box.scrollHeight
  }, [messages])

  async function open(id) {
    tailAbort.current?.abort()
    setCid(id)
    if (!id) { setMessages([]); setThreadAs(''); return }
    const r = await api(`/api/conversations/${id}/messages`)
    setMessages(r.messages)
    setThreadAs(r.agent_slug || '')
    if (!r.running) return
    // a turn is still executing server-side — re-attach and watch it finish,
    // seeding the placeholder with the tool calls it already made
    setBusy(true)
    const seed = (r.pending_activity || []).map((a) => ({ kind: 'tool', ...a }))
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true, parts: seed }])
    const ctl = new AbortController()
    tailAbort.current = ctl
    try {
      await tailStream(`/api/chat/${id}/stream`, (ev) => {
        if (ev.type === 'idle') {
          api(`/api/conversations/${id}/messages`).then((r2) => setMessages(r2.messages))
          return
        }
        handleTurnEvent(ev)
      }, ctl.signal)
    } catch { /* tail aborted; messages reload on next open */ }
    setBusy(false)
  }

  async function stop() {
    // ends the turn server-side; the tail's final "[Request interrupted]"
    // event settles the UI through the normal finish path
    if (!cid) return
    try { await api(`/api/chat/${cid}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }

  async function send() {
    const text = input.trim()
    if (!text || busy) return
    setBusy(true)
    // clear the bar NOW — the message visibly left; it comes back on failure
    setInput('')
    const wasNew = cid === null
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true, parts: [] }])
    try {
      await chatStream(
        // a NEW conversation is created pre-pinned to this board's project, so
        // even its first turn runs in the right context (the old post-hoc PATCH
        // raced the turn's project resolution)
        // identity binds the same way: new conversations only (the backend
        // ignores it on an existing one, and 404s an unknown slug)
        { message: text, conversation_id: cid,
          project: wasNew && projectSlug ? projectSlug : undefined,
          agent: wasNew && newAs ? newAs : undefined,
          permission_mode: wasNew ? permMode : undefined },
        (ev) => {
          if (ev.type === 'start') {
            setCid(ev.conversation_id)
            if (wasNew) setThreadAs(ev.agent_slug || newAs)
            if (wasNew && projectSlug) refresh()
          }
          handleTurnEvent(ev)
        },
      )
      if (!projectSlug) refresh()
    } catch (err) {
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'turn_in_progress') {
        setInput(text)
        setMessages((m) => [...m, { role: 'error',
          content: 'a turn is still running in this chat — wait for it to finish' }])
      } else {
        setInput(text)
        setMessages((m) => [...m, { role: 'error', content: err.detail || String(err) }])
      }
    }
    setBusy(false)
  }

  const current = convos.find((c) => c.id === cid)
  const who = cid ? threadAs : newAs
  const nameOf = (slug) => agents.find((a) => a.slug === slug)?.name || slug
  const whoName = who ? nameOf(who) : 'Jav3'
  return (
    <div className="chatbox">
      <div className="row cb-head">
        <button className="ghost" title="past chats"
                onClick={() => setShowHistory((s) => !s)}>☰ {convos.length}</button>
        <Tag tone={who ? 'running' : undefined} className="cb-who"
             title={who ? `this thread runs as the ${whoName} agent` : 'central Jav3'}>
          {whoName}</Tag>
        <span className="grow ellipsis dim">
          {current ? (current.summary || `#${current.id}`) : 'new chat'}</span>
        <PermissionModeSelect cid={cid} value={permMode} onChange={setPermMode} />
        <Menu floating open={newMenu} onClose={() => setNewMenu(false)} width={260} className="cb-new-menu"
              label="start a new chat as"
              trigger={(
                <Button variant="ghost" aria-haspopup="menu" aria-expanded={newMenu}
                        title="new chat — as Jav3 or an agent"
                        onClick={() => setNewMenu((o) => !o)}>+ new ▾</Button>
              )}>
          <MenuItem onClick={() => newChat('')}>Jav3</MenuItem>
          {agents.length > 0 && <MenuSep />}
          {agents.map((a) => (
            <MenuItem key={a.slug} sub={a.description || undefined}
                      onClick={() => newChat(a.slug)}>{a.name}</MenuItem>
          ))}
        </Menu>
      </div>
      {showHistory && (
        <ul className="cb-history">
          {convos.length === 0 && <EmptyState as="li">no past chats yet</EmptyState>}
          {convos.map((c) => (
            <li key={c.id} className={c.id === cid ? 'active' : ''}
                onClick={() => pick(c.id)}>
              <span className="grow ellipsis">
                {c.summary || `#${c.id} · ${c.started_at?.slice(5, 16) || ''}`}</span>
              {c.agent_slug && <Tag>{nameOf(c.agent_slug)}</Tag>}
              <button className="win-btn" title="delete" onClick={(e) => del(c.id, e)}>×</button>
            </li>
          ))}
        </ul>
      )}
      <div className="messages compact">
        {messages.length === 0 && (
          <EmptyState pad>
            {projectSlug ? `chat with ${whoName} about this project` : 'say hi'}
          </EmptyState>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.role === 'assistant'
              ? <MessageBody m={m} />
              : <pre>{m.content || (m.streaming ? '…' : '')}</pre>}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <AskPanel asks={asks} cid={cid} compact />
      <form className="row" onSubmit={(e) => { e.preventDefault(); send() }}>
        <textarea className="grow" rows={2} value={input}
                  placeholder={`message ${whoName}…`}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                  }} />
        {busy
          ? <button type="button" className="ghost danger" title="stop this turn"
                    onClick={stop}>⏹</button>
          : <button type="submit">↑</button>}
      </form>
    </div>
  )
}
