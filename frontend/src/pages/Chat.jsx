import { useEffect, useRef, useState } from 'react'
import { api, chatStream } from '../api.js'

export default function Chat() {
  const [conversations, setConversations] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(null)
  const bottomRef = useRef(null)

  useEffect(() => {
    api('/api/conversations').then((r) => setConversations(r.conversations))
    api('/api/projects').then((r) => setActive(r.active))
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function openConversation(id) {
    setConversationId(id)
    const r = await api(`/api/conversations/${id}/messages`)
    setMessages(r.messages)
  }

  function newConversation() {
    setConversationId(null)
    setMessages([])
  }

  async function send(confirmPeak = false) {
    const text = input.trim()
    if (!text || busy) return
    setBusy(true)
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true }])
    try {
      await chatStream(
        { message: text, conversation_id: conversationId, confirm_peak: confirmPeak },
        (ev) => {
          if (ev.type === 'start') setConversationId(ev.conversation_id)
          if (ev.type === 'token')
            setMessages((m) => {
              const copy = [...m]
              const last = copy[copy.length - 1]
              copy[copy.length - 1] = { ...last, content: last.content + ev.text }
              return copy
            })
          if (ev.type === 'tool')
            setMessages((m) => {
              const copy = [...m]
              const last = copy[copy.length - 1]
              copy[copy.length - 1] = {
                ...last,
                content: last.content + `\n[tool: ${ev.name}]\n`,
              }
              return copy
            })
          if (ev.type === 'final')
            setMessages((m) => {
              const copy = [...m]
              copy[copy.length - 1] = { role: 'assistant', content: ev.content }
              return copy
            })
          if (ev.type === 'error')
            setMessages((m) => {
              const copy = [...m]
              copy[copy.length - 1] = { role: 'error', content: ev.message }
              return copy
            })
        },
      )
      setInput('')
      api('/api/conversations').then((r) => setConversations(r.conversations))
    } catch (err) {
      // drop the two optimistic messages; a peak-retry re-adds them
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        if (window.confirm('Peak pricing right now — 2x cost. Use the API?')) {
          if (err.conversationId && !conversationId)
            setConversationId(Number(err.conversationId))
          setBusy(false)
          await send(true)
          return
        }
      } else {
        setMessages((m) => [...m, { role: 'error', content: err.detail || String(err) }])
      }
    }
    setBusy(false)
  }

  return (
    <div className="chat-layout">
      <aside>
        <button onClick={newConversation}>+ New chat</button>
        <ul className="convo-list">
          {conversations.map((c) => (
            <li key={c.id} className={c.id === conversationId ? 'active' : ''}
                onClick={() => openConversation(c.id)}>
              #{c.id} · {c.started_at?.slice(0, 16) || ''}
            </li>
          ))}
        </ul>
        {active && <div className="active-project">project loaded: {active}</div>}
      </aside>
      <main>
        <div className="messages">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <pre>{m.content || (m.streaming ? '…' : '')}</pre>
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
          <button type="submit" disabled={busy}>{busy ? '…' : 'Send'}</button>
        </form>
      </main>
    </div>
  )
}
