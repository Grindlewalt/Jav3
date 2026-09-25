import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { isPhone, useIsPhone } from '../breakpoints.js'
import { notify, notifyError } from '../notify.js'
import { useChatStream } from '../useChatStream.js'
import { listTitle } from '../ChatGroups.jsx'
import ShellSidebar from './ShellSidebar.jsx'
import Transcript from './Transcript.jsx'
import Composer from './Composer.jsx'
import ScopeChip from './ScopeChip.jsx'
import { useApprovals } from './approvals.js'
import Dock, { tabsFor } from './Dock.jsx'

// The terminal-style shell: sidebar · transcript · (dock, M2).
//
// One chat is the whole screen. A project is not a place you go but a scope a
// chat runs in — /shell is a global chat, /shell/p/:slug a project's home (a
// fresh chat pinned to it), /shell/c/:id one conversation. It is one route
// (`/shell/*`) parsed here rather than three, so moving between them never
// remounts the shell and never drops a live stream.
//
// Data: one GET /api/sidebar feeds the sidebar AND this component's view of
// the open chat's row (title, project, agent); it refreshes after every turn
// and on a slow poll so "Working" tracks turns started elsewhere.

const EXPAND_KEY = 'jarvis.shell.expand'
const DOCK_KEY = 'jarvis.shell.dock'
const SIDEBAR_POLL_MS = 10000

function parseRest(rest) {
  const [kind, arg] = (rest || '').split('/')
  if (kind === 'c' && /^\d+$/.test(arg || '')) return { cid: Number(arg), slug: null }
  if (kind === 'p' && arg) return { cid: null, slug: decodeURIComponent(arg) }
  return { cid: null, slug: null }
}

// the dock opens closed on a first visit; after that it remembers
function readDock() {
  try {
    const d = JSON.parse(localStorage.getItem(DOCK_KEY)) || {}
    return { open: !!d.open, tab: d.tab || 'files', w: Number(d.w) || 480 }
  } catch { return { open: false, tab: 'files', w: 480 } }
}

function readExpand() {
  try { return localStorage.getItem(EXPAND_KEY) === '1' } catch { return false }
}

export default function Shell() {
  const params = useParams()
  const { cid, slug } = parseRest(params['*'])
  const navigate = useNavigate()
  const ask = useAsk()
  const phone = useIsPhone()

  const [side, setSide] = useState(null)         // GET /api/sidebar
  const [agents, setAgents] = useState([])
  const [input, setInput] = useState('')
  const [temporary, setTemporary] = useState(false)
  const [newAs, setNewAs] = useState('')         // agent slug for a fresh chat
  const [threadAgent, setThreadAgent] = useState(null)
  const [expandAll, setExpandAll] = useState(readExpand)
  const [drawer, setDrawer] = useState(false)    // phone: the sidebar sheet
  // a sheet over the whole transcript never greets you on a phone
  const [dock, setDock] = useState(() => {
    const d = readDock()
    return isPhone() ? { ...d, open: false } : d
  })
  const [chatJobs, setChatJobs] = useState([])   // jobs this chat launched (/messages)

  const chat = useChatStream()
  const { messages, busy, peakAsk, setPeakAsk, openThread, stopTurn, runTurn,
          handleTurnEvent } = chat
  const liveId = useRef(null)      // id of the turn in flight (a temp chat never adopts it)
  const adopted = useRef(null)     // id a live send just put in the URL — don't reopen it
  const pendingAs = useRef('')     // "new chat as <agent>" surviving the navigation

  const refreshSide = useCallback(
    () => api('/api/sidebar').then(setSide).catch(() => {}), [])

  useEffect(() => {
    refreshSide()
    api('/api/agents').then((r) => setAgents(r.agents || [])).catch(() => {})
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') refreshSide()
    }, SIDEBAR_POLL_MS)
    return () => clearInterval(t)
  }, [refreshSide])

  useEffect(() => {
    try { localStorage.setItem(EXPAND_KEY, expandAll ? '1' : '0') } catch { /* private mode */ }
  }, [expandAll])
  useEffect(() => {
    try { localStorage.setItem(DOCK_KEY, JSON.stringify(dock)) } catch { /* private mode */ }
  }, [dock])
  const openDock = useCallback((tab) => setDock((d) => ({ ...d, open: true, tab: tab || d.tab })), [])

  // The URL is the source of truth for what is open. A send that creates a
  // conversation writes its id into the URL itself; that one is already on
  // screen and streaming, so it is not reopened.
  useEffect(() => {
    if (cid != null && cid === adopted.current) return
    adopted.current = null
    setPeakAsk(null)
    setTemporary(false)
    setThreadAgent(null)
    setChatJobs([])
    if (cid == null) { openThread(null); return }
    openThread(cid, { onTailDone: refreshSide })
      .then((r) => { if (r) { setThreadAgent(r.agent_slug || null); setChatJobs(r.jobs || []) } })
      .catch(notifyError)
  }, [cid, openThread, refreshSide, setPeakAsk])

  // arriving at a fresh chat resets its choices — except the agent a "new chat
  // as…" asked for on the way here
  useEffect(() => {
    if (cid != null) return
    setNewAs(pendingAs.current)
    pendingAs.current = ''
    setInput('')
  }, [cid, slug])

  // /messages answers for any id — a deleted chat comes back as an empty
  // transcript that looks new but 404s on the first send. The sidebar list is
  // the authority on what exists; a chat a send just created is exempt until
  // the list catches up.
  useEffect(() => {
    if (!side || cid == null || cid === adopted.current) return
    if (side.conversations.some((c) => c.id === cid)) return
    notify('That chat no longer exists.')
    navigate('/shell', { replace: true })
  }, [side, cid, navigate])

  const convo = side?.conversations.find((c) => c.id === cid) || null
  const projects = side?.projects || []
  const fresh = cid == null
  const agentSlug = fresh ? newAs : (threadAgent ?? convo?.agent_slug ?? null)
  const agentName = (s) => agents.find((a) => a.slug === s)?.name || s
  const scopeSlug = fresh ? slug : (convo?.project_slug || null)
  // pinned to a project that has since been deleted: say so on the chip and
  // don't aim the sandbox line or the dock at its endpoints (they 404)
  const scopeGone = !fresh && !!convo?.project_gone
  const liveSlug = scopeGone ? null : scopeSlug

  const open = (id) => navigate(`/shell/c/${id}`)
  const openProject = (s) => navigate(`/shell/p/${encodeURIComponent(s)}`)
  // on a phone the sidebar is a sheet over the transcript: anything that
  // navigates (a chat, a project row, ＋) should reveal what it opened
  useEffect(() => { setDrawer(false) }, [cid, slug])
  // Escape backs out of the phone's sheets, like every other popover
  useEffect(() => {
    if (!phone || !(drawer || dock.open)) return undefined
    const onKey = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      if (drawer) setDrawer(false)
      else setDock((d) => ({ ...d, open: false }))
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [phone, drawer, dock.open])
  // a new chat stays in the scope you are in: from a project's chat, the
  // project's home; from a global one, /shell
  function newChat(asAgent = '') {
    setDrawer(false)
    pendingAs.current = asAgent
    setNewAs(asAgent)          // already on that fresh URL: no effect will run
    setTemporary(false)
    navigate(scopeSlug ? `/shell/p/${encodeURIComponent(scopeSlug)}` : '/shell')
  }

  async function renameChat(c) {
    const next = await ask.prompt('Rename this chat', c.summary || '',
                                  { confirmLabel: 'Rename' })
    if (!next?.trim()) return
    try {
      await api(`/api/conversations/${c.id}`, {
        method: 'PATCH', body: JSON.stringify({ title: next.trim() }) })
    } catch (err) { notifyError(err) }
    refreshSide()
  }

  async function deleteChat(c) {
    if (!await ask.confirm(`Delete “${listTitle(c)}”?`,
                           { confirmLabel: 'Delete', danger: true })) return
    try {
      await api(`/api/conversations/${c.id}`, { method: 'DELETE' })
    } catch (err) { notifyError(err); return }
    if (c.id === cid) navigate('/shell', { replace: true })
    refreshSide()
  }

  async function setScope(nextSlug) {
    if (fresh) {
      navigate(nextSlug ? `/shell/p/${encodeURIComponent(nextSlug)}` : '/shell')
      return
    }
    try {
      await api(`/api/conversations/${cid}`, {
        method: 'PATCH',
        body: JSON.stringify(nextSlug ? { project: nextSlug, mode: 'pin' }
                                      : { project: null, mode: 'none' }) })
    } catch (err) { notifyError(err) }
    refreshSide()
  }

  function onTurnEvent(ev) {
    if (ev.type === 'start') {
      liveId.current = ev.conversation_id
      setThreadAgent(ev.agent_slug || null)
      if (!temporary && cid == null) {
        adopted.current = ev.conversation_id
        navigate(`/shell/c/${ev.conversation_id}`, { replace: true })
        refreshSide()
      }
    }
    handleTurnEvent(ev)
  }

  async function send(confirmPeak = false, resend = null) {
    const text = (resend ?? input).trim()
    if (!text || busy) return
    if (!resend) setInput('')
    const body = {
      message: text, conversation_id: cid, confirm_peak: confirmPeak,
      ephemeral: fresh && temporary,
      // only meaningful when this turn creates the conversation. The shell
      // has two scopes, not three: pinned to the project in the URL, or to no
      // project (files land in the chat's own store) — never "whatever is
      // loaded globally", which is how chats silently inherited the wrong one.
      ...(fresh ? { project: slug || null, project_mode: slug ? 'pin' : 'none',
                    ...(newAs ? { agent: newAs } : {}) } : {}),
    }
    await runTurn({
      text, body, onEvent: onTurnEvent,
      onDone: refreshSide, onRestoreDraft: setInput,
    })
  }

  const stop = () => stopTurn(cid ?? liveId.current)

  const title = fresh
    ? (slug ? (projects.find((p) => p.slug === slug)?.name || slug) : 'New chat')
    : (convo ? listTitle(convo) : `Chat #${cid}`)
  const hasSteps = messages.some((m) => m.activity?.length)

  // what is waiting on the operator in this chat's scope, shown inline. A
  // chat the sidebar list hasn't caught up with yet has no known scope.
  const approvalScope = fresh ? slug : (convo ? (convo.project_slug || `chat-${cid}`) : null)
  const scopeNeed = side?.needs.find((n) => n.scope === approvalScope)
  const approvals = useApprovals({
    scope: approvalScope, isProject: !!scopeSlug, busy,
    poke: scopeNeed ? `${scopeNeed.git}/${scopeNeed.egress}/${scopeNeed.plan}` : '',
    onChanged: refreshSide,
  })

  // the dock follows the scope: the project, else this chat's own store
  const dockSlug = liveSlug || (cid != null ? `chat-${cid}` : null)
  const dockTab = tabsFor(!!liveSlug).includes(dock.tab) ? dock.tab : 'files'

  return (
    <div className={`shell${drawer ? ' drawer-open' : ''}`}>
      <ShellSidebar side={side} activeId={cid} activeSlug={fresh ? slug : null}
                    projectSlug={liveSlug}
                    agentName={agentName} onOpen={open} onOpenProject={openProject}
                    onNew={() => newChat('')}
                    onRename={renameChat} onDelete={deleteChat}
                    onChanged={refreshSide} onClose={() => setDrawer(false)} />
      {drawer && <div className="shell-scrim" onClick={() => setDrawer(false)} />}
      <main className="shell-main">
        <header className="sh-head">
          {phone && (
            <button type="button" className="icon-btn" aria-label="chats"
                    title="chats" onClick={() => setDrawer(true)}>☰</button>
          )}
          <span className="sh-title ellipsis" title={convo?.summary || title}>{title}</span>
          <div className="sh-head-meta">
            {agentSlug && (
              <span className="sh-agent" title={`this chat runs as the ${agentName(agentSlug)} agent`}>
                @{agentSlug}</span>
            )}
            <ScopeChip projects={projects} value={scopeSlug} gone={scopeGone}
                       onPick={setScope} />
            <button type="button" className="sh-toggle sh-panels" aria-pressed={dock.open}
                    title={dock.open ? 'close the panels' : 'files, git, plan, terminal, network, runs'}
                    onClick={() => setDock((d) => ({ ...d, open: !d.open }))}>Panels</button>
            {hasSteps && (
              <button type="button" className="sh-toggle" aria-pressed={expandAll}
                      title={expandAll ? 'fold every turn’s steps' : 'show every turn’s steps'}
                      onClick={() => setExpandAll((x) => !x)}>
                {expandAll ? 'Fold steps' : 'Expand steps'}</button>
            )}
          </div>
        </header>
        <Transcript cid={cid} messages={messages} expandAll={expandAll} fresh={fresh}
                    slug={slug} side={side} agentName={agentSlug ? agentName(agentSlug) : 'Jav3'}
                    temporary={temporary} onOpen={open} approvals={approvals}
                    onReview={() => openDock('git')} />
        <Composer value={input} onChange={setInput} onSend={() => send()}
                  busy={busy} onStop={stop}
                  peakAsk={peakAsk}
                  onPeakCancel={() => { setInput(peakAsk); setPeakAsk(null) }}
                  onPeakConfirm={() => { const t = peakAsk; setPeakAsk(null); send(true, t) }}
                  fresh={fresh} agents={agents} agentSlug={agentSlug}
                  agentName={agentName} onPickAgent={setNewAs}
                  onNewAs={(s) => newChat(s)}
                  temporary={temporary} onTemporary={setTemporary}
                  history={messages} />
      </main>
      {dock.open && phone && (
        <div className="shell-scrim dock-scrim" onClick={() => setDock((d) => ({ ...d, open: false }))} />
      )}
      {dock.open && (
        <Dock slug={dockSlug} isProject={!!liveSlug} chatJobs={chatJobs}
              tab={dockTab} onTab={(t) => setDock((d) => ({ ...d, tab: t }))}
              onClose={() => setDock((d) => ({ ...d, open: false }))}
              width={dock.w} onWidth={(w) => setDock((d) => ({ ...d, w }))} />
      )}
    </div>
  )
}
