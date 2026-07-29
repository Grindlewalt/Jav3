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
  // on a phone the list is an overlay, so it starts closed unless the operator
  // has explicitly opened it before; on desktop it stays open by default
  const [sideOpen, setSideOpen] = useState(() => {
    const saved = localStorage.getItem('jarvis.chat.side')
    if (saved) return saved !== 'closed'
    return !window.matchMedia('(max-width: 768px)').matches
  })
  const scrollRef = useRef(null)   // the .messages scroll container
  const inputRef = useRef(null)
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
    localStorage.setItem('jarvis.chat.side', sideOpen ? 'open' : 'closed')
  }, [sideOpen])

  // incognito greys out the whole GUI: .incog on <html> swaps the palette
  // variables, and a transient .theme-fade class makes every surface
  // cross-fade instead of snapping
  useEffect(() => {
    const el = document.documentElement
    el.classList.add('theme-fade')
    el.classList.toggle('incog', incognito)
    const t = setTimeout(() => el.classList.remove('theme-fade'), 700)
    return () => clearTimeout(t)
  }, [incognito])
  // leaving the Chat page ends the unsaved chat — restore the normal palette
  useEffect(() => () =>
    document.documentElement.classList.remove('incog', 'theme-fade'), [])

  useEffect(() => {
    // scroll only the message list, never the page (scrollIntoView walks
    // every scrollable ancestor)
    const box = scrollRef.current
    if (box) box.scrollTop = box.scrollHeight
  }, [messages])

  // the composer grows with the draft, up to the CSS max-height
  function autoGrow() {
    const ta = inputRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }
  useEffect(autoGrow, [input])

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

  // the phone list overlays the thread, so picking a chat should reveal it
  const closeSideOnPhone = () => {
    if (window.matchMedia('(max-width: 768px)').matches) setSideOpen(false)
  }

  async function openConversation(id) {
    tailAbort.current?.abort()
    setIncognito(false)   // saved chats always use the normal palette
    closeSideOnPhone()
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
    setIncognito(false)   // a fresh chat always starts saved + light
    closeSideOnPhone()
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
      <aside className={sideOpen ? '' : 'collapsed'}>
        <div className="side-head">
          <button type="button" className="icon-btn"
                  title={sideOpen ? 'collapse sidebar' : 'expand sidebar'}
                  onClick={() => setSideOpen((o) => !o)}>{sideOpen ? '«' : '»'}</button>
          <span className="side-title">Chats</span>
          <button type="button" className="icon-btn" title="new chat"
                  onClick={newConversation}>＋</button>
        </div>
        <ul className="convo-list">
          {conversations.map((c) => (
            <li key={c.id} className={c.id === conversationId ? 'active' : ''}
                onClick={() => openConversation(c.id)}>
              {/* title owns the row; the project slug sits under it so a long
                  slug can never crush the title into two letters */}
              <div className="convo-main">
                <span className="convo-title ellipsis" title={c.summary || `#${c.id}`}>
                  {c.summary || `#${c.id} · ${c.started_at?.slice(5, 16) || ''}`}</span>
                {c.project_slug && <span className="convo-proj ellipsis">{c.project_slug}</span>}
              </div>
              <button className="win-btn" title="delete chat"
                      onClick={(e) => { e.stopPropagation(); deleteConversation(c.id) }}>×</button>
            </li>
          ))}
        </ul>
        {active && <div className="active-project">project loaded: {active}</div>}
      </aside>
      {sideOpen && (
        <div className="chat-scrim" onClick={() => setSideOpen(false)} />
      )}
      <main>
        {/* on a phone the list is off-canvas, and its collapse button goes
            with it — this is the way back to it */}
        <div className="chat-mobile-bar">
          <button type="button" className="icon-btn" aria-label="open chat list"
                  onClick={() => setSideOpen(true)}>☰</button>
          <span className="grow ellipsis">
            {conversationId
              ? (conversations.find((c) => c.id === conversationId)?.summary
                 || `Chat #${conversationId}`)
              : 'New chat'}
          </span>
          <button type="button" className="icon-btn" aria-label="new chat"
                  onClick={newConversation}>＋</button>
        </div>
        {!conversationId && incognito && messages.length > 0 && (
          <div className="chat-toolbar">
            <span className="tag incog-tag">🕶 incognito</span>
            <span className="dim small">nothing here is saved — closing this chat discards it</span>
          </div>
        )}
        {conversationId && (
          <div className="chat-toolbar">
            <span className="chat-title ellipsis">
              {conversations.find((c) => c.id === conversationId)?.summary
                || `Chat #${conversationId}`}
            </span>
            <span className="tag">#{conversationId}</span>
            <span className="grow" />
            <label className="dim">project</label>
            <select
              value={conversations.find((c) => c.id === conversationId)?.project_slug || ''}
              onChange={(e) => assignProject(e.target.value)}>
              <option value="">— none —</option>
              {projects.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
            </select>
          </div>
        )}
        <div className="messages" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="chat-empty">
              <div className="orb" />
              <h2>How can I help?</h2>
              <p>{incognito ? 'Incognito — this chat won’t be saved.'
                : 'Enter to send · Shift+Enter for a newline'}</p>
              <div className="chat-suggest">
                {['What did my schedules do overnight?',
                  'Summarize the latest security events',
                  'What changed across my projects this week?'].map((s) => (
                  <button key={s} type="button"
                          onClick={() => { setInput(s); inputRef.current?.focus() }}>
                    {s}
                  </button>
                ))}
              </div>
              <label className="incog-switch"
                     title="off: incognito — nothing is saved and the GUI greys out; recovery is SSH-only">
                <input type="checkbox" checked={!incognito}
                       onChange={(e) => setIncognito(!e.target.checked)} />
                <span className="track"><span className="knob">{incognito ? '🕶' : ''}</span></span>
                <span className="incog-label">
                  {incognito ? 'Incognito — nothing will be saved' : 'Chat is saved'}</span>
              </label>
            </div>
          ) : (
            <div className="thread">
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role}`}>
                  {m.role === 'assistant' ? <>
                    <div className={`msg-avatar ${m.streaming ? 'thinking' : ''}`}>J</div>
                    <MessageBody m={m} />
                  </> : <pre>{m.content || (m.streaming ? '…' : '')}</pre>}
                </div>
              ))}
            </div>
          )}
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send() }}>
          <div className="composer-inner">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
              placeholder={incognito ? 'Message Jarvis (incognito)…' : 'Message Jarvis…'}
              rows={1}
            />
            {busy
              ? <button type="button" className="send-btn stop" title="stop this turn"
                        onClick={stop}>◼</button>
              : <button type="submit" className="send-btn" title="send"
                        disabled={!input.trim()}>↑</button>}
          </div>
        </form>
      </main>
    </div>
  )
}
