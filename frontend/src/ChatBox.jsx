import { useEffect, useRef, useState } from 'react'
import { api, chatStream, tailStream } from './api.js'
import { applyTurnEvent, finishTurn, MessageBody } from './ToolActivity.jsx'

// Compact chat, embeddable anywhere (board panel). When projectSlug is set,
// conversations are filtered to that project and new ones are linked to it.
export default function ChatBox({ projectSlug }) {
  const [convos, setConvos] = useState([])
  const [cid, setCid] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [showHistory, setShowHistory] = useState(false)
  const bottomRef = useRef(null)
  const tailAbort = useRef(null)   // cancels a resume-tail on switch/unmount

  useEffect(() => () => tailAbort.current?.abort(), [])

  // shared by the live POST stream and a resumed background-turn tail
  function handleTurnEvent(ev) {
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
      .then((r) => setConvos(r.conversations))
  useEffect(() => { refresh() }, [projectSlug]) // eslint-disable-line

  async function pick(id) {
    setShowHistory(false)
    await open(id)
  }

  function newChat() {
    setShowHistory(false)
    setCid(null)
    setMessages([])
  }

  async function del(id, e) {
    e.stopPropagation()
    if (!window.confirm(`delete chat #${id}?`)) return
    await api(`/api/conversations/${id}`, { method: 'DELETE' })
    if (id === cid) newChat()
    refresh()
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function open(id) {
    tailAbort.current?.abort()
    setCid(id)
    if (!id) { setMessages([]); return }
    const r = await api(`/api/conversations/${id}/messages`)
    setMessages(r.messages)
    if (!r.running) return
    // a turn is still executing server-side — re-attach and watch it finish
    setBusy(true)
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true, parts: [] }])
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

  async function send(confirmPeak = false) {
    const text = input.trim()
    if (!text || busy) return
    setBusy(true)
    const wasNew = cid === null
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true, parts: [] }])
    try {
      await chatStream(
        { message: text, conversation_id: cid, confirm_peak: confirmPeak },
        (ev) => {
          if (ev.type === 'start') {
            setCid(ev.conversation_id)
            if (wasNew && projectSlug)
              api(`/api/conversations/${ev.conversation_id}`, {
                method: 'PATCH',
                body: JSON.stringify({ project: projectSlug }),
              }).then(refresh)
          }
          handleTurnEvent(ev)
        },
      )
      setInput('')
      if (!projectSlug) refresh()
    } catch (err) {
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        // a new conversation doesn't exist yet on this 409 (the backend
        // gates before creating it), so the retry just re-sends confirmed
        if (window.confirm('Peak pricing right now — 2x cost. Use the API?')) {
          setBusy(false)
          await send(true)
          return
        }
      } else if (err.status === 409 && err.detail === 'turn_in_progress') {
        setMessages((m) => [...m, { role: 'error',
          content: 'a turn is still running in this chat — wait for it to finish' }])
      } else {
        setMessages((m) => [...m, { role: 'error', content: err.detail || String(err) }])
      }
    }
    setBusy(false)
  }

  const current = convos.find((c) => c.id === cid)
  return (
    <div className="chatbox">
      <div className="row cb-head">
        <button className="ghost" title="past chats"
                onClick={() => setShowHistory((s) => !s)}>☰ {convos.length}</button>
        <span className="grow ellipsis dim">
          {current ? (current.summary || `#${current.id}`) : 'new chat'}</span>
        <button className="ghost" title="new chat" onClick={newChat}>+ new</button>
      </div>
      {showHistory && (
        <ul className="cb-history">
          {convos.length === 0 && <li className="dim">no past chats yet</li>}
          {convos.map((c) => (
            <li key={c.id} className={c.id === cid ? 'active' : ''}
                onClick={() => pick(c.id)}>
              <span className="grow ellipsis">
                {c.summary || `#${c.id} · ${c.started_at?.slice(5, 16) || ''}`}</span>
              <button className="win-btn" title="delete" onClick={(e) => del(c.id, e)}>×</button>
            </li>
          ))}
        </ul>
      )}
      <div className="messages compact">
        {messages.length === 0 && (
          <div className="dim center-pad">
            {projectSlug ? 'chat with Jarvis about this project' : 'say hi'}
          </div>
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
      <form className="row" onSubmit={(e) => { e.preventDefault(); send() }}>
        <textarea className="grow" rows={2} value={input}
                  placeholder="message Jarvis…"
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                  }} />
        <button type="submit" disabled={busy}>{busy ? '…' : '↑'}</button>
      </form>
    </div>
  )
}
