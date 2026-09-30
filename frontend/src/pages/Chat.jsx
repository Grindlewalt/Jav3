import {
  Fragment, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState,
} from 'react'
import { api, chatStream, tailStream } from '../api.js'
import { NavSlotContext } from '../nav.jsx'
import { useDismiss } from '../useDismiss.js'
import { onProjectsChanged } from '../projectsChanged.js'
import { isPhone, useIsPhone } from '../breakpoints.js'
import { MessageBody } from '../ToolActivity.jsx'
import { activityMark, makeTurnFolder, newTurn, seedParts } from '../turnEvents.js'
import { useDockHeight, useFollow } from '../useFollow.js'
import TurnStatus from '../TurnStatus.jsx'
import { useAsk } from '../ask.jsx'
import ModelPicker from '../ModelPicker.jsx'
import { AskPanel, PermissionModeSelect, useOperatorAsks, usePermissionMode } from '../AskUser.jsx'
import ChatGroups from '../ChatGroups.jsx'
import { useSlash } from '../slash/useSlash.jsx'
import { subscribe } from '../events.js'
import { useApprovals, placeApprovals } from '../shell/approvals.js'
import ApprovalRow from '../shell/ApprovalRow.jsx'
import RunsTab from '../shell/RunsTab.jsx'

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

// Work (work/Work.jsx) hosts this page and adds its windows beside it. Every
// prop is optional, so Chat on its own renders exactly as it always has:
//   toolbarExtra     rendered at the end of the chat toolbar (Work's +)
//   beside           rendered after <main>, inside .chat-layout (the windows)
//   onProjectChange  called with the slug this chat's work goes to, or null
//   controlRef       filled with { pickProject(slug), openProject(slug) }
const NEEDS_POLL_MS = 15000

export default function Chat({
  toolbarExtra = null, beside = null, onProjectChange, controlRef,
} = {}) {
  const [conversations, setConversations] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [active, setActive] = useState(null)
  const [projects, setProjects] = useState([])
  const [greeting, setGreeting] = useState(pickGreeting)
  const [multiline, setMultiline] = useState(false)   // composer past one line
  const ask = useAsk()
  // project chosen for a chat that doesn't exist yet; sent with the first turn
  const [pendingProject, setPendingProject] = useState('')
  const [pendingMode, setPendingMode] = useState('follow')
  // a temporary chat persists nothing: no conversation row, no transcript.
  // Doesn't repaint the GUI — the toolbar switch and the composer placeholder
  // carry it.
  const [temporary, setTemporary] = useState(false)
  // the multi-agent jobs this chat launched (/messages `jobs`), and whether
  // the Runs view is standing in for the transcript
  const [chatJobs, setChatJobs] = useState([])
  const [runsView, setRunsView] = useState(false)
  // bumped when something may have joined the approval queue (see below)
  const [needsPoke, setNeedsPoke] = useState(0)
  const asks = useOperatorAsks(conversationId, busy)   // ask_user / permission asks
  const [permMode, setPermMode] = usePermissionMode(conversationId)
  // on a phone the list is an overlay, so it starts closed unless the operator
  // has explicitly opened it before; on desktop it stays open by default
  const [sideOpen, setSideOpen] = useState(() => {
    const saved = localStorage.getItem('jarvis.chat.side')
    if (saved) return saved !== 'closed'
    return !isPhone()
  })
  // Collapsed on a desktop, the sidebar stops being a chat list and becomes the
  // app's nav rail: it publishes a mount point and App portals the destinations
  // into it. On a phone the sidebar is off-canvas, so it can't hold the nav —
  // no slot there, and the top bar stays.
  const phone = useIsPhone()
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
  // Which chat this view is showing, as a counter: opening or leaving a chat
  // bumps it, and a stream that started under an earlier value has been
  // superseded. Without it a turn still streaming from chat A wrote its tokens,
  // its final reply and its busy flag into whichever chat had been opened since.
  const gen = useRef(0)
  const dockRef = useRef(null)     // the composer's dock: status, asks, the bar
  const folder = useRef(null)
  if (!folder.current) folder.current = makeTurnFolder((fn) => setMessages(fn))
  // Read during render, before any effect: the persistence effect below fires
  // on mount with a null id and would clear the key before a restore effect
  // could read it.
  const resumeId = useRef(Number(localStorage.getItem('jarvis.chat.last')) || null)

  const refreshConvos = () =>
    api('/api/conversations').then((r) => setConversations(r.conversations))

  useEffect(() => {
    api('/api/conversations').then((r) => {
      setConversations(r.conversations)
      // Resume only a chat that is still in the list. The messages endpoint
      // answers for any id — a deleted one comes back as an empty transcript,
      // which looks exactly like a new chat but is bound to a conversation the
      // backend 404s on the first send.
      const id = resumeId.current
      // A resume is not a pick: the phone sheet may be open because the
      // drawer's "Chat history" row just asked for it, and closing it here
      // shut the list the operator had opened a frame earlier.
      if (id && r.conversations.some((c) => c.id === id)) openConversation(id, { resume: true })
      else localStorage.removeItem('jarvis.chat.last')
    }).catch(() => {})
    api('/api/projects').then((r) => { setActive(r.active); setProjects(r.projects) })
    // the Projects sheet made a change: the sidebar's Projects list is this copy
    const offProjects = onProjectsChanged(() => {
      api('/api/projects').then((r) => { setActive(r.active); setProjects(r.projects) })
        .catch(() => {})
    })
    return () => { offProjects(); tailAbort.current?.abort() }
  }, [])

  // "Chat" in the nav is a destination, not a reset. It used to mount a blank
  // conversation every time, so coming back from Projects silently dropped the
  // chat you were in — and it did the same job as ＋, which is the control that
  // actually means "new". Resuming makes the two distinct: ＋ is now the only
  // way to start one.
  useEffect(() => {
    if (conversationId) localStorage.setItem('jarvis.chat.last', String(conversationId))
    else localStorage.removeItem('jarvis.chat.last')
  }, [conversationId])


  useEffect(() => {
    localStorage.setItem('jarvis.chat.side', sideOpen ? 'open' : 'closed')
  }, [sideOpen])

  // the phone drawer's "Chat history" row (App.jsx) when Chat is already open
  useEffect(() => {
    const open = () => setSideOpen(true)
    window.addEventListener('jarvis-open-chats', open)
    return () => window.removeEventListener('jarvis-open-chats', open)
  }, [])

  // Starting a chat doesn't replace the orb, it moves it: the big one is the
  // same object as the little one beside Jav3's first reply. send() measures
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

  // Follow the stream to the bottom unless the reader has scrolled up (then a
  // "N new" pill brings them back). Only the message list scrolls, never the
  // page (scrollIntoView walks every scrollable ancestor).
  const follow = useFollow(scrollRef, { mark: activityMark(messages), resetKey: conversationId })
  const followRun = follow.follow
  useLayoutEffect(() => { followRun() }, [messages, followRun])
  useDockHeight(dockRef)

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

  // One handler for both paths: the live POST stream and a resumed tail.
  // token/tool/tool_result/job fold into the streaming message's parts (text
  // tokens batched: turnEvents.makeTurnFolder); final swaps in the reply with
  // the activity collapsed above it; an error keeps the run and appends itself.
  // `mine` is the counter value the stream started under: once the view has
  // moved to another chat (gen bumped) the stream is stale and says nothing.
  function handleTurnEvent(ev, mine = gen.current) {
    if (mine !== gen.current) return
    slash.onEvent(ev)
    asks.onEvent(ev)
    if (ev.type === 'start') {
      liveId.current = ev.conversation_id
      // temporary: never adopt the id, or the chat becomes a saved one
      if (!temporary) {
        setConversationId(ev.conversation_id)
        adoptConversation(ev.conversation_id)
      }
    }
    folder.current.handle(ev)
  }

  // A chat this turn just created is not in the list until the list is
  // fetched, and until then everything that reads the chat's project from its
  // row saw a chat that follows the loaded project: the windows beside the chat
  // swapped to the WRONG project's board for the whole turn, and back when it
  // ended. Give the row what the send asked for now, then fetch the real one.
  function adoptConversation(id) {
    setConversations((cs) => (cs.some((c) => c.id === id) ? cs : [{
      id, summary: '', running: true,
      project_slug: pendingMode === 'pin' ? (pendingProject || null) : null,
      project_locked: pendingMode === 'follow' ? 0 : 1,
    }, ...cs]))
    refreshConvos()
  }

  // the phone list overlays the thread, so picking a chat should reveal it
  const closeSideOnPhone = () => {
    if (isPhone()) setSideOpen(false)
  }

  async function openConversation(id, { resume = false, attempt = 0 } = {}) {
    tailAbort.current?.abort()
    folder.current.cancel()
    const mine = ++gen.current
    // the id answers and stops go to: it stayed on the chat that last STARTED a
    // turn, so a question from the chat opened since was answered into the old one
    liveId.current = id
    setTemporary(false)   // saved chats always persist
    if (!resume) closeSideOnPhone()
    setConversationId(id)
    setChatJobs([])
    setBusy(false)        // an abandoned send or tail no longer speaks for this view
    const r = await api(`/api/conversations/${id}/messages`)
    // a slow transcript for a chat the reader already left must not land over
    // the one they went to
    if (mine !== gen.current) return
    setMessages(r.messages)
    setChatJobs(r.jobs || [])
    if (!r.running) return
    // a turn is still executing server-side — re-attach and watch it finish,
    // seeding the placeholder with the tool calls it already made
    setBusy(true)
    const seed = seedParts(r)
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true,
                                parts: seed, t0: Date.now() }])
    const ctl = new AbortController()
    tailAbort.current = ctl
    let settled = false
    try {
      await tailStream(`/api/chat/${id}/stream`, (ev) => {
        if (ev.type === 'idle') {
          // turn ended between the messages fetch and the tail — reload
          settled = true
          api(`/api/conversations/${id}/messages`).then((r2) => {
            if (mine === gen.current) setMessages(r2.messages)
          })
          return
        }
        if (ev.type === 'final' || ev.type === 'error') settled = true
        handleTurnEvent(ev, mine)
      }, ctl.signal)
    } catch { /* tail aborted or dropped */ }
    if (mine !== gen.current) return
    folder.current.flush()
    if (!settled && attempt < 3) {
      // the stream ended without an ending, so the turn may well still be
      // going (a dropped connection): look again rather than leave a spinner
      // that never resolves
      await new Promise((ok) => { setTimeout(ok, 800 * (attempt + 1)) })
      if (mine === gen.current) openConversation(id, { resume: true, attempt: attempt + 1 })
      return
    }
    if (!settled) {
      // out of patience: show what is saved instead of a placeholder that
      // would spin forever
      try {
        const r2 = await api(`/api/conversations/${id}/messages`)
        if (mine === gen.current) setMessages(r2.messages)
      } catch { /* offline: the placeholder stays until the next open */ }
    }
    setBusy(false)
    refreshConvos()
  }

  function newConversation() {
    tailAbort.current?.abort()
    folder.current.cancel()
    gen.current += 1
    liveId.current = null
    setBusy(false)
    closeSideOnPhone()
    setConversationId(null)
    setMessages([])
    setChatJobs([])
    setRunsView(false)
    setPendingProject('')
    setPendingMode('follow')
    setTemporary(false)
    setGreeting(pickGreeting())
  }

  async function renameConversation(id, current) {
    const next = await ask.prompt('Rename this chat', current || '',
                                  { confirmLabel: 'Rename' })
    if (next === null) return
    if (!next.trim()) return
    await api(`/api/conversations/${id}`, {
      method: 'PATCH', body: JSON.stringify({ title: next.trim() }) })
    refreshConvos()
  }

  async function deleteConversation(id) {
    // unlike projects, agents and schedules, a chat has no bin: it is gone
    if (!await ask.confirm(`Permanently delete chat #${id}?`,
                           { body: 'Its messages and tool history are removed. This cannot be undone.',
                             confirmLabel: 'Delete forever', danger: true })) return
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

  // The project this chat's work goes to right now: a saved chat's pin (or
  // the loaded project while it follows), a new chat's pending choice.
  const effectiveProject = conversationId
    ? (openConvo?.project_slug
       || (convoMode(openConvo) === 'follow' ? active : null) || null)
    : (pendingMode === 'pin' ? (pendingProject || null)
       : pendingMode === 'follow' ? (active || null) : null)
  useEffect(() => { onProjectChange?.(effectiveProject) },
            [effectiveProject, onProjectChange])
  if (controlRef) {
    controlRef.current = {
      // the pill's "pin to this project", from outside
      pickProject: (slug) => {
        if (conversationId) return assignProject('pin', slug)
        setPendingMode('pin')
        setPendingProject(slug)
      },
      // a /projects/:slug link: a new chat pinned to it, unless this chat is
      // already there (or still empty, when it just takes the pin)
      openProject: (slug) => {
        resumeId.current = null   // a cold load: don't let the resume win
        if (slug === effectiveProject) return
        if (conversationId || messages.length) newConversation()
        setPendingMode('pin')
        setPendingProject(slug)
      },
    }
  }

  async function stop() {
    // the turn ends server-side and every tail gets a final "[Request
    // interrupted]" event — the normal finish path settles the UI
    const id = conversationId ?? liveId.current
    if (!id) return
    try { await api(`/api/chat/${id}/stop`, { method: 'POST' }) } catch { /* already done */ }
    asks.settle()   // the turn's open question is void now; do not leave a dead card
  }

  async function send(resend = null) {
    const text = (resend ?? input).trim()
    if (!text || busy) return
    // the orb is on screen only while the chat is empty — grab where it is
    // before this turn unmounts it, so the avatar can fly in from there
    if (messages.length === 0 && orbRef.current)
      orbFrom.current = orbRef.current.getBoundingClientRect()
    const mine = gen.current
    let started = false   // the turn began (a `start` arrived)
    let settled = false   // it ended (a `final` or `error` arrived)
    setBusy(true)
    // clear the bar NOW — the message visibly left; it comes back on failure
    if (!resend) setInput('')
    setMessages((m) => [...m, ...newTurn(text)])
    follow.jump()   // sending is a request to see the answer
    try {
      await chatStream(
        { message: text, conversation_id: conversationId,
          ephemeral: temporary,
          // only meaningful when the conversation is being created by this turn
          ...(conversationId ? {} : { project: pendingProject || null,
                                      project_mode: pendingMode, ...slash.newChatFields,
                                      permission_mode: permMode }) },
        (ev) => {
          if (ev.type === 'start') started = true
          if (ev.type === 'final' || ev.type === 'error') settled = true
          handleTurnEvent(ev, mine)
        },
      )
    } catch (err) {
      if (mine !== gen.current) return   // the reader moved on; the turn runs regardless
      folder.current.flush()
      if (started && !err.status) {
        // the connection dropped mid-turn: the turn is still running server-side
        settled = false
      } else {
        // refused before anything streamed: drop the two optimistic messages
        setMessages((m) => m.slice(0, -2))
        setInput(text)
        setMessages((m) => [...m, { role: 'error',
          content: err.status === 409 && err.detail === 'turn_in_progress'
            ? 'a turn is still running in this chat — wait for it to finish'
            : (err.detail || String(err)) }])
        setBusy(false)
        return
      }
    }
    if (mine !== gen.current) { refreshConvos(); return }
    folder.current.flush()
    if (!settled) {
      // The stream ended without an ending (a dropped connection, a restart).
      // Rather than leave a placeholder spinning, pick the turn back up by
      // re-attaching; a temporary chat has nothing saved to re-attach to.
      const id = conversationId ?? liveId.current
      if (id && !temporary) { openConversation(id, { resume: true }); return }
      folder.current.handle({ type: 'error', message: 'the connection dropped mid-turn' })
    }
    setBusy(false)
    api('/api/conversations').then((r) => setConversations(r.conversations))
    // a turn may have launched research, a team or a plan run: refresh the
    // Runs view's "this chat" list (a temporary chat has nothing to fetch)
    const done = conversationId ?? liveId.current
    if (done && !temporary) {
      api(`/api/conversations/${done}/messages`)
        .then((r) => setChatJobs(r.jobs || [])).catch(() => {})
    }
  }

  // Swipe in from the left edge to open the chat list on a phone — the quick
  // path; the visible one is the drawer's "Chat history" row. The
  // start must be at the screen's edge so horizontal scrolls inside code
  // blocks and tables never trigger it.
  const edgeTouch = useRef(null)
  const onEdgeTouchStart = (e) => {
    if (!phone || sideOpen) { edgeTouch.current = null; return }
    const t = e.touches[0]
    edgeTouch.current = t.clientX <= 28 ? { x: t.clientX, y: t.clientY } : null
  }
  const onEdgeTouchMove = (e) => {
    const s = edgeTouch.current
    if (!s) return
    const t = e.touches[0]
    if (t.clientX - s.x > 42 && Math.abs(t.clientY - s.y) < 40) {
      setSideOpen(true)
      edgeTouch.current = null
    } else if (Math.abs(t.clientY - s.y) >= 40) edgeTouch.current = null
  }

  // a brand-new chat: no saved conversation, nothing sent yet. On a phone the
  // toolbar disappears and its two controls move under the orb instead.
  const fresh = !conversationId && messages.length === 0
  const projectPicker = (
    <ProjectPicker
      projects={projects} global={active}
      mode={conversationId ? convoMode(openConvo) : pendingMode}
      value={conversationId ? (openConvo?.project_slug || '') : pendingProject}
      onPick={(mode, slug) => {
        if (conversationId) return assignProject(mode, slug)
        setPendingMode(mode)
        setPendingProject(slug)
      }} />
  )
  const tempSwitch = (
    <label className={`temp-switch${temporary ? ' on' : ''}`}
           title="temporary chat: nothing is written to disk, and it is gone once you leave">
      <input type="checkbox" checked={temporary}
             onChange={(e) => setTemporary(e.target.checked)} />
      <span className="temp-track"><span className="temp-knob" /></span>
      <span className="temp-word">Temporary chat</span>
    </label>
  )

  // What is waiting on the operator in this chat's scope, inline in the
  // transcript as /shell shows it (shell/approvals.js). The scope is the
  // project this chat's work goes to, else a saved chat's own `chat-<id>`
  // store. The queue reloads on open, every few seconds while a turn runs,
  // and on `poke`: an egress/notices event on the ONE shared stream
  // (events.js), plus a slow poll while the tab is visible for git and plan
  // requests, which have no topic of their own. Never a new EventSource.
  const approvalScope = effectiveProject
    || (conversationId && !temporary ? `chat-${conversationId}` : null)
  useEffect(() => {
    if (!approvalScope) return undefined
    const bump = () => setNeedsPoke((n) => n + 1)
    const offs = [subscribe('egress', bump), subscribe('notices', bump)]
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') bump()
    }, NEEDS_POLL_MS)
    return () => { offs.forEach((off) => off()); clearInterval(t) }
  }, [approvalScope])
  const approvals = useApprovals({
    scope: approvalScope, isProject: !!effectiveProject, busy, poke: needsPoke,
  })
  const { after: approvalsAfter, tail: approvalsTail } =
    placeApprovals(messages, approvals.items)
  const approvalRows = (list) => list.map((a) => (
    <ApprovalRow key={a.key} a={a} acting={approvals.acting} onDecide={approvals.decide} />
  ))
  // Runs is offered wherever there can be runs: a saved chat, or a project
  const runsOffered = !!(conversationId || effectiveProject) && !temporary
  const showRuns = runsView && runsOffered
  const runningJobs = chatJobs.filter((j) => j.running).length

  const slash = useSlash({
    input, setInput, busy, conversationId, turnId: () => conversationId ?? liveId.current,
    conversations, projects, active, pendingProject, messages, setMessages,
    temporary, setTemporary, send: (t) => send(t), newChat: newConversation,
    openChat: openConversation, openList: () => setSideOpen(true), stop,
    pickProject: (mode, slug) => (conversationId ? assignProject(mode, slug)
      : (setPendingMode(mode), setPendingProject(slug || ''))),
    rename: () => renameConversation(conversationId, openConvo?.summary),
    deleteChat: () => deleteConversation(conversationId), refresh: refreshConvos,
    refreshProjects: () => api('/api/projects').then((r) => { setActive(r.active); setProjects(r.projects) }),
  })

  return (
    <div className="chat-layout"
         onTouchStart={onEdgeTouchStart} onTouchMove={onEdgeTouchMove}>
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
        <ChatGroups conversations={conversations} activeId={conversationId}
                    projects={projects} onOpen={openConversation}
                    onRename={(c) => renameConversation(c.id, c.summary)}
                    onDelete={(c) => deleteConversation(c.id)}
                    onChanged={refreshConvos} />
      </aside>
      {sideOpen && (
        <div className="chat-scrim" onClick={() => setSideOpen(false)} />
      )}
      <main onMouseMove={trackGlow} onMouseLeave={fadeGlow}>
        {/* the one home for the project state — except a fresh chat on a
            phone, where both controls sit under the orb and the bar itself
            would just be an empty strip */}
        {(!phone || !fresh) && (
          <div className="chat-toolbar">
            {conversationId
              ? <span className="chat-title ellipsis">
                  {openConvo?.summary || `Chat #${conversationId}`}</span>
              : <span className="grow" />}
            {/* the number and the project travel as one compact group, so the
                title is what gives way on a narrow screen */}
            <div className="chat-meta">
              {conversationId && <span className="tag">#{conversationId}</span>}
              {projectPicker}
              {/* beside the project control: both answer "where does this
                  turn's work go". A switch, not a chip — it is a mode you
                  leave set. Only offered while it can still apply: a saved
                  chat can't retroactively not exist, and a disabled switch
                  read as broken while crowding the phone toolbar. */}
              {!conversationId && tempSwitch}
              <PermissionModeSelect cid={conversationId} value={permMode} onChange={setPermMode} />
              {runsOffered && (
                <button type="button" className={`chat-runs-toggle${showRuns ? ' on' : ''}`}
                        aria-pressed={showRuns}
                        title={showRuns ? 'back to the conversation'
                          : 'research, agent teams and plan runs around this chat'}
                        onClick={() => setRunsView((v) => !v)}>
                  {showRuns ? 'Chat' : 'Runs'}
                  {!showRuns && runningJobs > 0 && (
                    <span className="chat-runs-count">{runningJobs}</span>)}
                </button>
              )}
              {toolbarExtra}
            </div>
          </div>
        )}
        {showRuns && (
          <div className="chat-runs-view">
            <RunsTab project={effectiveProject} chatJobs={chatJobs} />
          </div>
        )}
        {/* hidden, not unmounted, under the Runs view: the scroll position
            and the streaming placeholder survive a look at the runs */}
        <div className="messages" ref={scrollRef} hidden={showRuns}>
          {messages.length === 0 ? (
            <div className="chat-empty">
              <div className="orb" ref={orbRef} />
              {/* the chat page's only heading, so it is the <h1> */}
              <h1>{greeting}</h1>
              {phone && fresh && (
                <div className="empty-controls">
                  {projectPicker}
                  {tempSwitch}
                </div>
              )}
              {approvalsTail.length > 0 && (
                <div className="chat-approvals">{approvalRows(approvalsTail)}</div>
              )}
            </div>
          ) : (
            <div className="thread">
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role}${m.midturn ? ` midturn ${m.midturn}` : ''}`}>
                  {m.role === 'assistant' ? <>
                    <div className={`msg-avatar ${m.streaming ? 'thinking' : ''}`} />
                    <MessageBody m={m} />
                  </> : <pre>{m.content || (m.streaming ? '…' : '')}</pre>}
                </div>
              )).map((row, i) => (approvalsAfter.has(i)
                ? <Fragment key={i}>{row}<div className="chat-approvals">
                    {approvalRows(approvalsAfter.get(i))}</div></Fragment>
                : row))}
              {approvalsTail.length > 0 && (
                <div className="chat-approvals">{approvalRows(approvalsTail)}</div>
              )}
            </div>
          )}
        </div>
        {/* The dock: the composer floats over the bottom of the thread, and
            what has to be seen while a turn runs rides with it: the steady
            status line, an open question, the way back to the newest.
            (Between the list and the composer, an open question sat UNDER the
            composer's overlay, its buttons out of reach.) */}
        <form className="composer" ref={dockRef}
              onSubmit={(e) => { e.preventDefault(); slash.submit() || send() }}>
          <div className="composer-glow" ref={glowRef} />
          {slash.popup}
          {!follow.pinned && (follow.away > 0 || busy) && !showRuns && messages.length > 0 && (
            <button type="button" className="follow-pill" onClick={follow.jump}>
              {follow.away > 0 ? `${follow.away} new` : 'latest'} <span aria-hidden="true">↓</span>
            </button>
          )}
          <TurnStatus busy={busy} messages={messages} cid={liveId.current ?? conversationId}
                      waiting={asks.asks.length > 0} />
          <AskPanel asks={asks} cid={liveId.current ?? conversationId} />
          <div className={`composer-inner${multiline ? ' multi' : ''}`}>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (slash.onKeyDown(e)) return
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
              }}
              // a phone's bar is ~230px of text: the long form wrapped onto a
              // second line the one-row box then clipped
              placeholder={temporary
                ? (phone ? 'Temporary chat…' : 'Message Jav3 (temporary chat)…')
                : 'Message Jav3…'}
              rows={1}
            />
            <ModelPicker chip="model-chip" wrap="composer-model" />
            {busy
              ? <button type="button" className="send-btn stop" title="stop this turn"
                        onClick={stop}>◼</button>
              : <button type="submit" className="send-btn" title="send"
                        disabled={!input.trim()}>↑</button>}
          </div>
        </form>
      </main>
      {beside}
    </div>
  )
}
