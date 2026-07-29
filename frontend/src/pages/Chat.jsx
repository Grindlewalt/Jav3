import {
  useCallback, useContext, useEffect, useLayoutEffect, useRef, useState,
} from 'react'
import { api, chatStream, tailStream } from '../api.js'
import { NavSlotContext } from '../App.jsx'
import { useDismiss } from '../useDismiss.js'
import { applyTurnEvent, finishTurn, MessageBody } from '../ToolActivity.jsx'

// Empty-state greeting, swapped in per new chat. Mostly not about the time of
// day — a handful per period nod to it (capped at 5) so it doesn't read as a
// gimmick that's always talking about the clock.
const GREETINGS = {
  morning: [
    'Morning, sir.',
    'Good morning — try not to open forty tabs before breakfast.',
    'Early start, sir?',
    "The coffee's fresh; so is the morning queue.",
    'Up with the sun, or fighting it?',
    "Right then — where do we begin?",
    'Standing by, as ever.',
    'Systems nominal. You, less certain — go on then.',
    "Another queue, another day. Let's clear it.",
    "I've been awake the whole time. You get the excuse.",
    'At your service, sir.',
    "Whenever you're ready.",
    "Let's make today's list somebody else's problem.",
    "You bring the questions, I'll bring the follow-through.",
    'First request — no pressure.',
    'Onwards.',
    "I've kept the seat warm.",
    "Let's not overthink the first ten minutes.",
    'Say the word.',
    "No fires so far. Let's keep it that way.",
    'Fresh terminal, clean slate.',
    'Shall we?',
    "I've been idling productively.",
    'Consider me caffeinated in spirit, if nothing else.',
  ],
  midday: [
    'Halfway through the day and still unbothered.',
    'Afternoon lull? Not on my watch.',
    "Midday check-in — what's on the docket?",
    "The day's second half starts now.",
    'Post-lunch fog is a you problem, not a me problem.',
    'Say the word.',
    'Standing by.',
    "What's next on the list?",
    "I've been idling productively.",
    'Right, what\'s the crisis today?',
    "You've survived the hard part. Onwards.",
    "Let's turn 'later' into 'done'.",
    'Go on, then.',
    "I'm listening.",
    "Whatever's next, I'm across it.",
    'Ready and, dare I say, a little bored.',
    'Consider me at your disposal.',
    'One task or twelve — makes no difference to me.',
    "Shall we get on with it?",
    'Feed me a problem.',
    'Still here. Still capable.',
    "Momentum's a fragile thing. Let's not lose it.",
    "Whatever you're stuck on, I probably have opinions.",
    'Your move, sir.',
  ],
  night: [
    'Burning those midnight tokens?',
    'Still up, I see.',
    'Night owl mode: engaged.',
    "The world's asleep. We're not.",
    'Late one, sir?',
    'No judgment. Just data.',
    "Let's make this quick and painless.",
    "I don't sleep, so I don't mind.",
    "Let's get this sorted so you can actually rest.",
    'At your service, whatever the hour.',
    'Quiet hours, focused work.',
    "You're here. I'm here. Let's not waste it.",
    'Say the word.',
    'Fewer distractions right now, at least.',
    'Onwards, into the quiet.',
    "I'll keep the lights on, figuratively.",
    'Whenever inspiration strikes, apparently.',
    'No rush. Also, definitely some rush.',
    "Let's be efficient about this.",
    'The house is quiet. Good time to think.',
    "I've got nowhere else to be.",
    'Consider me undistracted.',
    "Let's wrap this up before it wraps around you.",
    "Whatever's keeping you up, let's make it worth it.",
  ],
}

function pickGreeting() {
  const h = new Date().getHours()
  const period = h < 5 ? 'night' : h < 12 ? 'morning' : h < 18 ? 'midday' : 'night'
  const list = GREETINGS[period]
  return list[Math.floor(Math.random() * list.length)]
}

// The glassy flash/pro picker at the send end of the composer. It changes the
// same server-side setting as the nav switch (they sync over the
// jarvis-model-changed window event) and only shows while the draft is empty —
// the menu opens upward, since the bar lives at the bottom of the screen.
const MODEL_SUB = {
  flash: 'fast · everyday',
  pro: 'deeper reasoning · ~3× price',
}

function ComposerModel({ visible }) {
  const [m, setM] = useState(null)
  const [open, setOpen] = useState(false)
  useEffect(() => {
    api('/api/model').then(setM).catch(() => {})
    const h = (e) => setM(e.detail)
    window.addEventListener('jarvis-model-changed', h)
    return () => window.removeEventListener('jarvis-model-changed', h)
  }, [])
  const close = useCallback(() => setOpen(false), [])
  const ref = useDismiss(open, close)
  if (!m) return null
  const short = (id) => id.replace(/^deepseek-v4-/, '')
  async function pick(model) {
    setOpen(false)
    if (model === m.active) return
    try {
      const next = await api('/api/model', {
        method: 'PUT', body: JSON.stringify({ model }) })
      setM(next)
      window.dispatchEvent(new CustomEvent('jarvis-model-changed', { detail: next }))
    } catch (err) { window.alert(err.detail || String(err)) }
  }
  return (
    <div className={`composer-model${visible ? '' : ' gone'}`} ref={ref}>
      <button type="button" className="model-chip" aria-haspopup="menu"
              aria-expanded={open} title="model for new turns"
              onClick={() => setOpen((o) => !o)}>
        {short(m.active)}
        <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
      </button>
      {open && (
        <div className="model-menu" role="menu">
          {m.choices.map((c) => (
            <button key={c} type="button" role="menuitemradio"
                    aria-checked={c === m.active} onClick={() => pick(c)}>
              <span className="m-name">{short(c)}
                <span className="m-sub">{MODEL_SUB[short(c)] || c}</span></span>
              {c === m.active && <span className="m-check" aria-hidden="true">●</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// The project control for the open chat, in the same glassy idiom as the model
// chip. It also absorbed the old "project loaded: x" line that used to sit in
// bright green in the sidebar's bottom corner, so there is one place project
// state is read instead of two.
//
// Three states, not two. "follow" is the historic default — the chat uses
// whatever project is loaded globally. "none" is a real answer: pinned to no
// project, so file work lands in the chat's own artifact store instead of
// silently inheriting the last project that happened to be open.
function ProjectPicker({ projects, mode, value, global: loaded, onPick }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const ref = useDismiss(open, close)
  const name = (slug) => projects.find((p) => p.slug === slug)?.name || slug
  const label = mode === 'pin' ? name(value)
    : mode === 'none' ? 'No project'
    : (loaded ? name(loaded) : 'No project')
  const pick = (m, slug) => { setOpen(false); onPick(m, slug || '') }
  return (
    <div className="proj-pick" ref={ref}>
      <button type="button" className="proj-chip" aria-haspopup="menu"
              aria-expanded={open}
              title={mode === 'follow'
                ? `following the loaded project${loaded ? ` (${loaded})` : ' — nothing loaded'}`
                : mode === 'none'
                  ? 'pinned to no project — files go to this chat’s artifacts'
                  : 'pinned to this project'}
              onClick={() => setOpen((o) => !o)}>
        <span className={`proj-dot${mode === 'pin' ? ' pinned' : ''}`
                         + (mode === 'none' ? ' none' : '')} aria-hidden="true" />
        <span className="ellipsis">{label}</span>
        {mode === 'follow' && loaded && <span className="proj-inherit">loaded</span>}
        <span className={open ? 'chev open' : 'chev'} aria-hidden="true">›</span>
      </button>
      {open && (
        <div className="proj-menu" role="menu">
          <button type="button" role="menuitemradio" aria-checked={mode === 'follow'}
                  onClick={() => pick('follow')}>
            <span className="m-name">Follow loaded project
              <span className="m-sub">{loaded ? name(loaded) : 'nothing loaded'}</span></span>
            {mode === 'follow' && <span className="m-check" aria-hidden="true">●</span>}
          </button>
          <button type="button" role="menuitemradio" aria-checked={mode === 'none'}
                  onClick={() => pick('none')}>
            <span className="m-name">No project
              <span className="m-sub">files go to this chat’s artifacts</span></span>
            {mode === 'none' && <span className="m-check" aria-hidden="true">●</span>}
          </button>
          <div className="proj-sep" />
          {projects.map((p) => (
            <button key={p.slug} type="button" role="menuitemradio"
                    aria-checked={mode === 'pin' && p.slug === value}
                    onClick={() => pick('pin', p.slug)}>
              <span className="m-name">{p.name}
                <span className="m-sub">{p.slug}</span></span>
              {mode === 'pin' && p.slug === value
                && <span className="m-check" aria-hidden="true">●</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Chat() {
  const [conversations, setConversations] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(null)
  const [projects, setProjects] = useState([])
  const [greeting, setGreeting] = useState(pickGreeting)
  const [multiline, setMultiline] = useState(false)   // composer past one line
  // project chosen for a chat that doesn't exist yet; sent with the first turn
  const [pendingProject, setPendingProject] = useState('')
  const [pendingMode, setPendingMode] = useState('follow')
  // a temporary chat persists nothing: no conversation row, no transcript.
  // Doesn't repaint the GUI — the toolbar switch and the composer placeholder
  // carry it.
  const [temporary, setTemporary] = useState(false)
  // on a phone the list is an overlay, so it starts closed unless the operator
  // has explicitly opened it before; on desktop it stays open by default
  const [sideOpen, setSideOpen] = useState(() => {
    const saved = localStorage.getItem('jarvis.chat.side')
    if (saved) return saved !== 'closed'
    return !window.matchMedia('(max-width: 768px)').matches
  })
  // Collapsed on a desktop, the sidebar stops being a chat list and becomes the
  // app's nav rail: it publishes a mount point and App portals the destinations
  // into it. On a phone the sidebar is off-canvas, so it can't hold the nav —
  // no slot there, and the top bar stays.
  const [phone, setPhone] = useState(
    () => window.matchMedia('(max-width: 768px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 768px)')
    const h = (e) => setPhone(e.matches)
    mq.addEventListener('change', h)
    return () => mq.removeEventListener('change', h)
  }, [])
  const setNavSlot = useContext(NavSlotContext)
  const slotRef = useCallback((el) => setNavSlot(el), [setNavSlot])
  // Hand the slot back when leaving Chat, or the bar never comes home. This is
  // a layout effect on purpose: as a passive one the release landed after the
  // next route had already painted, so there was a frame with the nav portaled
  // into a node that was no longer in the document.
  useLayoutEffect(() => () => setNavSlot(null), [setNavSlot])

  const scrollRef = useRef(null)   // the .messages scroll container
  const inputRef = useRef(null)
  const glowRef = useRef(null)     // composer underglow — direct style writes, not state
  const orbRef = useRef(null)      // empty-state orb, measured when a chat starts
  const orbFrom = useRef(null)     // its rect at that moment; consumed by the fly-in
  const tailAbort = useRef(null)   // cancels a resume-tail when switching chats
  const liveId = useRef(null)      // id of the turn in flight

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

  // Starting a chat doesn't replace the orb, it moves it: the big one is the
  // same object as the little one beside Jarvis's first reply. send() measures
  // it on the way out and this flies the avatar in from there, shrinking as it
  // goes. Runs on every message change but costs one null check unless a rect
  // is waiting.
  useLayoutEffect(() => {
    const from = orbFrom.current
    if (!from) return
    orbFrom.current = null
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const el = scrollRef.current?.querySelector('.msg.assistant .msg-avatar')
    if (!el) return
    const to = el.getBoundingClientRect()
    if (!to.width) return
    el.animate([
      { transform: `translate(${(from.left + from.width / 2) - (to.left + to.width / 2)}px, `
                 + `${(from.top + from.height / 2) - (to.top + to.height / 2)}px) `
                 + `scale(${from.width / to.width})` },
      { transform: 'none' },
    ], { duration: 620, easing: 'cubic-bezier(0.22, 0.9, 0.28, 1)' })
  }, [messages])

  useEffect(() => {
    // scroll only the message list, never the page (scrollIntoView walks
    // every scrollable ancestor)
    const box = scrollRef.current
    if (box) box.scrollTop = box.scrollHeight
  }, [messages])

  // the composer grows with the draft, up to the CSS max-height. Past one line
  // the pill relaxes into a rounded box — .multi is that threshold.
  function autoGrow() {
    const ta = inputRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
    setMultiline(ta.scrollHeight > 48)
  }
  useEffect(autoGrow, [input])

  // the composer's underglow pools wherever the cursor is: --gx is the point
  // along the bar the light gathers at, --gi how bright it burns (falling off
  // with the distance up the thread). Direct style writes for the same reason
  // as the orb — a mousemove must not render the message list.
  function trackGlow(e) {
    const el = glowRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    if (!r.width) return
    const x = ((e.clientX - r.left) / r.width) * 100
    const above = Math.max(0, r.top - e.clientY)      // 0 once level with the bar
    el.style.setProperty('--gx', `${Math.max(-12, Math.min(112, x))}%`)
    el.style.setProperty('--gi', Math.max(0, 1 - above / 300).toFixed(3))
  }
  function fadeGlow() {
    glowRef.current?.style.setProperty('--gi', '0')
  }

  // one handler for both paths: the live POST stream and a resumed tail.
  // token/tool/tool_result fold into the streaming message's parts; final
  // swaps in the reply with the activity collapsed above it.
  function handleTurnEvent(ev) {
    if (ev.type === 'start') {
      liveId.current = ev.conversation_id
      // temporary: never adopt the id, or the chat becomes a saved one
      if (!temporary) setConversationId(ev.conversation_id)
    }
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
    setTemporary(false)   // saved chats always persist
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
    closeSideOnPhone()
    setConversationId(null)
    setMessages([])
    setPendingProject('')
    setPendingMode('follow')
    setTemporary(false)
    setGreeting(pickGreeting())
  }

  async function deleteConversation(id) {
    if (!window.confirm(`delete chat #${id}?`)) return
    await api(`/api/conversations/${id}`, { method: 'DELETE' })
    if (id === conversationId) newConversation()
    refreshConvos()
  }

  // the row the toolbar is describing, and the picker state it implies:
  // a slug means pinned, a locked row with no slug means deliberately none,
  // anything else is still following whatever project is loaded
  const openConvo = conversations.find((c) => c.id === conversationId)
  const convoMode = (c) =>
    c?.project_slug ? 'pin' : (c?.project_locked ? 'none' : 'follow')

  async function assignProject(mode, slug) {
    await api(`/api/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ project: slug || null, mode }) })
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
    // the orb is on screen only while the chat is empty — grab where it is
    // before this turn unmounts it, so the avatar can fly in from there
    if (messages.length === 0 && orbRef.current)
      orbFrom.current = orbRef.current.getBoundingClientRect()
    setBusy(true)
    // clear the bar NOW — the message visibly left; it comes back on failure
    if (!resend) setInput('')
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true, parts: [] }])
    try {
      await chatStream(
        { message: text, conversation_id: conversationId, confirm_peak: confirmPeak,
          ephemeral: temporary,
          // only meaningful when the conversation is being created by this turn
          ...(conversationId ? {} : { project: pendingProject || null,
                                      project_mode: pendingMode }) },
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
        {!sideOpen && !phone && <div className="side-nav" ref={slotRef} />}
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
      </aside>
      {sideOpen && (
        <div className="chat-scrim" onClick={() => setSideOpen(false)} />
      )}
      <main onMouseMove={trackGlow} onMouseLeave={fadeGlow}>
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
        {/* always present, even on a fresh chat — it is the one home for the
            project state, so it can't come and go with the conversation */}
        <div className="chat-toolbar">
          {conversationId && <>
            <span className="chat-title ellipsis">
              {conversations.find((c) => c.id === conversationId)?.summary
                || `Chat #${conversationId}`}
            </span>
            <span className="tag">#{conversationId}</span>
          </>}
          <span className="grow" />
          <ProjectPicker
            projects={projects} global={active}
            mode={conversationId ? convoMode(openConvo) : pendingMode}
            value={conversationId ? (openConvo?.project_slug || '') : pendingProject}
            onPick={(mode, slug) => {
              if (conversationId) return assignProject(mode, slug)
              setPendingMode(mode)
              setPendingProject(slug)
            }} />
          {/* sits beside the project control: both answer "where does this
              turn's work go". A switch, not a chip — it is a mode you leave
              set, and the track shows which way at a glance. */}
          <label className={`temp-switch${temporary ? ' on' : ''}`
                            + (conversationId ? ' fixed' : '')}
                 title={conversationId
                   ? 'this chat is already saved — start a new one to make it temporary'
                   : 'temporary chat: nothing is written to disk, and it is gone '
                     + 'once you leave'}>
            <input type="checkbox" checked={temporary} disabled={!!conversationId}
                   onChange={(e) => setTemporary(e.target.checked)} />
            <span className="temp-track"><span className="temp-knob" /></span>
            <span className="temp-word">Temporary chat</span>
          </label>
        </div>
        <div className="messages" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="chat-empty">
              <div className="orb" ref={orbRef} />
              <h2>{greeting}</h2>
            </div>
          ) : (
            <div className="thread">
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role}`}>
                  {m.role === 'assistant' ? <>
                    <div className={`msg-avatar ${m.streaming ? 'thinking' : ''}`} />
                    <MessageBody m={m} />
                  </> : <pre>{m.content || (m.streaming ? '…' : '')}</pre>}
                </div>
              ))}
            </div>
          )}
        </div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); send() }}>
          <div className="composer-glow" ref={glowRef} />
          <div className={`composer-inner${multiline ? ' multi' : ''}`}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
              placeholder={temporary ? 'Message Jarvis (temporary chat)…'
                                     : 'Message Jarvis…'}
              rows={1}
            />
            <ComposerModel visible={!input.trim()} />
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
