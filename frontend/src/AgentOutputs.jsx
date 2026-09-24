import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, subscribeSse, tailStream } from './api.js'
import { notifyError } from './notify.js'
import { ts } from './format.js'
import Md from './Md.jsx'
import Button from './components/Button.jsx'
import Card from './components/Card.jsx'
import EmptyState from './components/EmptyState.jsx'
import Select from './components/Select.jsx'
import Tag from './components/Tag.jsx'
import Toolbar from './components/Toolbar.jsx'

// The Outputs tab (/agents/outputs, /agents/outputs/:slug): what agents
// produced, newest first — the threads that ran AS an agent plus everything
// descended from one (temp agents, funnel and research jobs it launched).
//
// Liveness without a poll. There is no "an output started" broadcast, so the
// list refreshes on the events that do exist: an operator-started run's
// completion notice (/api/agents/notices/stream), the end of any running
// row's own stream (tailed below, a few at a time), a job node spawning or
// finishing on a tailed job stream, and the tab coming back into view. A run
// started elsewhere while this tab sits idle shows up on the next of those.
const LIMIT = 100
const MAX_TAILS = 4    // each tail is a held connection; browsers cap ~6 per host

// which stream follows a running row, by kind
const tailUrl = (o) => ({
  agent: `/api/agents/runs/${o.id}/stream`,
  chat: `/api/chat/${o.id}/stream`,
  head: `/api/runs/${o.id}/stream`,
})[o.kind]

const stopUrl = (o) => ({
  agent: `/api/agents/runs/${o.id}/stop`,
  chat: `/api/chat/${o.id}/stop`,
})[o.kind]

// Chat resumes the conversation stored under this key on mount (Chat.jsx), and
// only if it is in its list — so this opens any chat-kind thread, which is
// exactly the set Chat can show.
const CHAT_RESUME_KEY = 'jarvis.chat.last'

// "[Name] task…" titles carry their own agent prefix; the card already says whose
const cleanTitle = (o) => (o.title || '').replace(/^\[[^\]]*\]\s*/, '') || `#${o.id}`

const rawHref = (project, path) =>
  `/api/projects/${encodeURIComponent(project)}/raw/${path.split('/').map(encodeURIComponent).join('/')}`

export default function AgentOutputs() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const [roster, setRoster] = useState([])
  const [outputs, setOutputs] = useState(null)
  const tailed = useRef(new Set())      // ids whose stream we already followed
  const timer = useRef(null)

  const load = useCallback(() => {
    const url = slug
      ? `/api/agents/${encodeURIComponent(slug)}/outputs?limit=${LIMIT}`
      : `/api/agents/outputs?limit=${LIMIT}`
    return api(url).then((r) => setOutputs(
      // the per-agent endpoint has no `owner`: every row there is this agent's
      slug ? r.outputs.map((o) => ({ ...o, owner: slug })) : r.outputs))
      .catch((err) => { setOutputs([]); notifyError(err) })
  }, [slug])

  // coalesce bursts (a research job spawns a dozen nodes in a second)
  const reload = useCallback(() => {
    clearTimeout(timer.current)
    timer.current = setTimeout(load, 400)
  }, [load])

  useEffect(() => {
    setOutputs(null)
    tailed.current = new Set()
    load()
    return () => clearTimeout(timer.current)
  }, [load])

  useEffect(() => {
    api('/api/agents').then((r) => setRoster(r.agents)).catch(() => {})
  }, [])

  // a finished operator-started run, and coming back to the tab
  useEffect(() => {
    const stop = subscribeSse('/api/agents/notices/stream', (ev) => {
      if (ev.type === 'agent_run_done' && (!slug || ev.slug === slug)) reload()
    })
    const onVis = () => { if (!document.hidden) reload() }
    document.addEventListener('visibilitychange', onVis)
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
  }, [slug, reload])

  // follow the running rows' own streams; each tail ending means that row
  // changed state. An id is tailed once — if the server still calls it
  // running after its stream closed, re-tailing would spin.
  const runningKey = (outputs || []).filter((o) => o.running && tailUrl(o))
    .map((o) => o.id).join(',')
  useEffect(() => {
    const rows = (outputs || []).filter((o) => o.running && tailUrl(o)
                                        && !tailed.current.has(o.id)).slice(0, MAX_TAILS)
    const tails = rows.map((o) => {
      tailed.current.add(o.id)
      const t = { id: o.id, ctl: new AbortController(), ended: false }
      tailStream(tailUrl(o), (ev) => {
        if (['node_spawned', 'node_done', 'job_final', 'final', 'error'].includes(ev.type))
          reload()
      }, t.ctl.signal).then(() => { t.ended = true; reload() }, () => {})
      return t
    })
    // a tail cut short here (the running set changed) never saw its row end,
    // so it may be picked up again; only a stream that closed on its own is spent
    return () => tails.forEach((t) => {
      if (!t.ended) tailed.current.delete(t.id)
      t.ctl.abort()
    })
  }, [runningKey]) // eslint-disable-line

  async function stopRun(o) {
    try {
      await api(stopUrl(o), { method: 'POST' })
      reload()
    } catch (err) { notifyError(err) }
  }

  function openInChat(id) {
    try { localStorage.setItem(CHAT_RESUME_KEY, String(id)) } catch { /* private mode */ }
    navigate('/')
  }

  const names = Object.fromEntries(roster.map((a) => [a.slug, a.name]))
  const nameOf = (s) => names[s] || s
  // deleted agents keep their past work, so the filter offers them too
  const owners = [...new Set([...roster.map((a) => a.slug),
                              ...(outputs || []).map((o) => o.owner).filter(Boolean),
                              ...(slug ? [slug] : [])])]
  const byId = Object.fromEntries((outputs || []).map((o) => [o.id, o]))
  // the thread a node belongs to, for "open in chat": itself if it is a chat,
  // else the nearest chat ancestor among the rows we have
  const chatOf = (o) => {
    for (let n = o, hops = 0; n && hops < 32; n = byId[n.parent_id], hops += 1)
      if (n.kind === 'chat') return n.id
    return null
  }

  return (
    <main className="editor-pane outputs-pane">
      <Toolbar className="outputs-bar">
        <Select aria-label="agent" value={slug}
                options={[{ value: '', label: 'all agents' },
                          ...owners.map((s) => ({ value: s, label: nameOf(s) }))]}
                onChange={(e) => navigate(e.target.value
                  ? `/agents/outputs/${encodeURIComponent(e.target.value)}`
                  : '/agents/outputs')} />
        <span className="grow" />
        {outputs && <span className="dim small">{outputs.length} shown</span>}
      </Toolbar>
      {outputs === null ? null : outputs.length === 0 ? (
        <EmptyState pad hint="an agent's threads, runs and the jobs it launches land here">
          {slug ? `${nameOf(slug)} hasn't produced anything yet` : 'no agent output yet'}
        </EmptyState>
      ) : (
        <div className="outputs-list">
          {outputs.map((o) => (
            <OutputCard key={o.id} o={o} owner={slug ? null : nameOf(o.owner)}
                        chatId={chatOf(o)} onOpenChat={openInChat}
                        onStop={stopUrl(o) ? () => stopRun(o) : null} />
          ))}
        </div>
      )}
    </main>
  )
}

function OutputCard({ o, owner, chatId, onOpenChat, onStop }) {
  const [transcript, setTranscript] = useState(null)
  const loadTranscript = (e) => {
    if (!e.currentTarget.open || transcript) return
    api(`/api/conversations/${o.id}/messages`)
      .then((r) => setTranscript((r.messages || [])
        .filter((m) => m.role === 'user' || m.role === 'assistant')))
      .catch(() => setTranscript([]))
  }
  return (
    <Card as="article" className={o.running ? 'output-card running' : 'output-card'}>
      <div className="output-head">
        <span className="output-title ellipsis" title={o.title || ''}>{cleanTitle(o)}</span>
        <Tag>{o.kind}</Tag>
        {o.running && <Tag tone="running">running</Tag>}
        {o.running && onStop && (
          <Button variant="ghost" danger onClick={onStop}>Stop</Button>)}
      </div>
      <div className="output-meta dim small">
        {owner && <span className="output-owner">{owner}</span>}
        <span>{ts(o.last_at || o.started_at)}</span>
        {o.project && (
          <Link to={`/projects/${encodeURIComponent(o.project)}`}>{o.project}</Link>)}
        {o.parent_id && <span>part of #{o.parent_id}</span>}
        <span className="grow" />
        {chatId && (
          <Button variant="link" onClick={() => onOpenChat(chatId)}>
            {chatId === o.id ? 'open in chat' : 'open its chat'}</Button>)}
      </div>
      {o.snippet && <p className="output-snippet">{o.snippet}</p>}
      {o.rollup && (
        <details className="output-fold">
          <summary>rollup</summary>
          <Md text={o.rollup} />
        </details>
      )}
      {o.kind !== 'chat' && (
        <details className="output-fold" onToggle={loadTranscript}>
          <summary>transcript</summary>
          {transcript === null ? <span className="dim small">loading…</span>
            : transcript.length === 0 ? <EmptyState>nothing recorded</EmptyState>
            : transcript.map((m, i) => (
              <div key={i} className={`output-msg ${m.role}`}>
                {m.role === 'assistant' ? <Md text={m.content} /> : <pre>{m.content}</pre>}
              </div>
            ))}
        </details>
      )}
      {o.runs_files?.length > 0 && (
        <ul className="output-files">
          {o.runs_files.map((f) => (
            <li key={f}>
              <a href={rawHref(o.project, f)} target="_blank" rel="noopener noreferrer">
                <code>{f}</code></a>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
