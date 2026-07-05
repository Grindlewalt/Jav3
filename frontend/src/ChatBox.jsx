import { useEffect, useRef, useState } from 'react'
import { api, chatStream } from './api.js'
import Md from './Md.jsx'

// Compact chat, embeddable anywhere (board panel). When projectSlug is set,
// conversations are filtered to that project and new ones are linked to it.
export default function ChatBox({ projectSlug }) {
  const [convos, setConvos] = useState([])
  const [cid, setCid] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef(null)

  const refresh = () =>
    api(`/api/conversations${projectSlug ? `?project=${encodeURIComponent(projectSlug)}` : ''}`)
      .then((r) => setConvos(r.conversations))
  useEffect(() => { refresh() }, [projectSlug]) // eslint-disable-line

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function open(id) {
    setCid(id)
    if (!id) { setMessages([]); return }
    const r = await api(`/api/conversations/${id}/messages`)
    setMessages(r.messages)
  }

  async function send(confirmPeak = false) {
    const text = input.trim()
    if (!text || busy) return
    setBusy(true)
    const wasNew = cid === null
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true }])
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
          if (ev.type === 'token')
            setMessages((m) => {
              const copy = [...m]
              const last = copy[copy.length - 1]
              copy[copy.length - 1] = { ...last, content: last.content + ev.text }
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
      if (!projectSlug) refresh()
    } catch (err) {
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        if (window.confirm('Peak pricing right now — 2x cost. Use the API?')) {
          if (err.conversationId && cid === null) setCid(Number(err.conversationId))
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
    <div className="chatbox">
      <div className="row">
        <select className="grow" value={cid || ''}
                onChange={(e) => open(e.target.value ? Number(e.target.value) : null)}>
          <option value="">new chat</option>
          {convos.map((c) => (
            <option key={c.id} value={c.id}>
              #{c.id} · {c.started_at?.slice(5, 16)}
            </option>
          ))}
        </select>
      </div>
      <div className="messages compact">
        {messages.length === 0 && (
          <div className="dim center-pad">
            {projectSlug ? 'chat with Jarvis about this project' : 'say hi'}
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.role === 'assistant'
              ? <div className="bubble"><Md text={m.content || (m.streaming ? '…' : '')} /></div>
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
