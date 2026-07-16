import { useEffect, useRef, useState } from 'react'
import { api, chatStream, tailStream } from '../api.js'
import { applyTurnEvent, finishTurn, MessageBody } from '../ToolActivity.jsx'

export default function Chat() {
  const [conversations, setConversations] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(null)
  const [projects, setProjects] = useState([])
  const [incognito, setIncognito] = useState(false)
  const bottomRef = useRef(null)
  const tailAbort = useRef(null)   // cancels a resume-tail when switching chats
  const liveId = useRef(null)      // id of the turn in flight (set even incognito)

  const refreshConvos = () =>
    api('/api/conversations').then((r) => setConversations(r.conversations))

  useEffect(() => {
    refreshConvos()
    api('/api/projects').then((r) => { setActive(r.active); setProjects(r.projects) })
    return () => tailAbort.current?.abort()
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // one handler for both paths: the live POST stream and a resumed tail.
  // token/tool/tool_result fold into the streaming message's parts; final
  // swaps in the reply with the activity collapsed above it.
  function handleTurnEvent(ev) {
    if (ev.type === 'start') liveId.current = ev.conversation_id
    if (ev.type === 'start' && !incognito) setConversationId(ev.conversation_id)
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

  async function openConversation(id) {
    tailAbort.current?.abort()
    setConversationId(id)
    const r = await api(`/api/conversations/${id}/messages`)
    setMessages(r.messages)
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
          // turn ended between the messages fetch and the tail — reload
          api(`/api/conversations/${id}/messages`).then((r2) => setMessages(r2.messages))
          return
        }
        handleTurnEvent(ev)
      }, ctl.signal)
      refreshConvos()
    } catch { /* tail aborted or dropped; messages reload on next open */ }
    setBusy(false)
  }

  function newConversation() {
    tailAbort.current?.abort()
    setBusy(false)
    setConversationId(null)
    setMessages([])
  }

  async function deleteConversation(id) {
    if (!window.confirm(`delete chat #${id}?`)) return
    await api(`/api/conversations/${id}`, { method: 'DELETE' })
    if (id === conversationId) newConversation()
    refreshConvos()
  }

  async function assignProject(slug) {
    await api(`/api/conversations/${conversationId}`, {
      method: 'PATCH', body: JSON.stringify({ project: slug || null }) })
    refreshConvos()
  }

  async function stop() {
    // the turn ends server-side and every tail gets a final "[Request
    // interrupted]" event — the normal finish path settles the UI
    const id = conversationId ?? liveId.current
    if (!id) return
    try { await api(`/api/chat/${id}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }

  async function send(confirmPeak = false, resend = null) {
    const text = (resend ?? input).trim()
    if (!text || busy) return
    setBusy(true)
    // clear the bar NOW — the message visibly left; it comes back on failure
    if (!resend) setInput('')
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true, parts: [] }])
    try {
      await chatStream(
        { message: text, conversation_id: conversationId, confirm_peak: confirmPeak,
          ephemeral: incognito },
        handleTurnEvent,
      )
      api('/api/conversations').then((r) => setConversations(r.conversations))
    } catch (err) {
      // drop the two optimistic messages; a peak-retry re-adds them
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        // a new conversation doesn't exist yet on this 409 (the backend
        // gates before creating it), so the retry just re-sends confirmed
        if (window.confirm('Peak pricing right now — 2x cost. Use the API?')) {
          setBusy(false)
          await send(true, text)
          return
        }
        setInput(text)   // declined: give the draft back
      } else if (err.status === 409 && err.detail === 'turn_in_progress') {
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

  return (
    <div className="chat-layout">
      <aside>
        <button onClick={newConversation}>+ New chat</button>
        <label className="incognito-toggle" title="persist nothing; memory writes go to a temp dir">
          <input type="checkbox" checked={incognito}
                 onChange={(e) => { setIncognito(e.target.checked); newConversation() }} />
          <span>🕶 incognito</span>
        </label>
        <ul className="convo-list">
          {conversations.map((c) => (
            <li key={c.id} className={c.id === conversationId ? 'active' : ''}
                onClick={() => openConversation(c.id)}>
              <span className="grow ellipsis" title={c.summary || `#${c.id}`}>
                {c.summary || `#${c.id} · ${c.started_at?.slice(5, 16) || ''}`}</span>
              {c.project_slug && <span className="tag">{c.project_slug}</span>}
              <button className="win-btn" title="delete chat"
                      onClick={(e) => { e.stopPropagation(); deleteConversation(c.id) }}>×</button>
            </li>
          ))}
        </ul>
        {active && <div className="active-project">project loaded: {active}</div>}
      </aside>
      <main>
        {conversationId && (
          <div className="chat-toolbar">
            <span className="dim">chat #{conversationId}</span>
            <label className="dim">project:</label>
            <select
              value={conversations.find((c) => c.id === conversationId)?.project_slug || ''}
              onChange={(e) => assignProject(e.target.value)}>
              <option value="">— none —</option>
              {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
            </select>
          </div>
        )}
        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.role === 'assistant'
                ? <MessageBody m={m} />
                : <pre>{m.content || (m.streaming ? '…' : '')}</pre>}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send() }}>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
            }}
            placeholder="Message Jarvis… (Enter to send, Shift+Enter for newline)"
            rows={3}
          />
          {busy
            ? <button type="button" className="ghost danger" title="stop this turn"
                      onClick={stop}>⏹ Stop</button>
            : <button type="submit">Send</button>}
        </form>
      </main>
    </div>
  )
}
