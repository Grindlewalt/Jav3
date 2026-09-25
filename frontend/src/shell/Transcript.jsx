import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import Md from '../Md.jsx'
import { ActivityGroup, MessageBody, ModelTag } from '../ToolActivity.jsx'
import { listTitle } from '../ChatGroups.jsx'
import { ago } from '../format.js'
import ApprovalRow from './ApprovalRow.jsx'
import { placeApprovals } from './approvals.js'

// One dense stream. The operator's turns are terminal prompt lines (`› …`, the
// only monospace here besides tool payloads); Jav3's replies are markdown at a
// readable measure; tool calls are the compact one-line rows ToolActivity
// already draws — live while a turn runs, folded into "N steps" once it ends
// unless the header's "Expand steps" is on.
//
// SECURITY: replies go through Md (sanitised); tool args/results only ever
// render inside <pre> via ToolRow, never through Md. The prompt line is the
// operator's own text, shown as text.

// Within this distance of the bottom counts as "reading the latest" — new
// output keeps the view pinned there. Scrolled further up, the operator is
// reading history and the stream must not yank them down.
const STICK_PX = 80

function Prompt({ text }) {
  return (
    <div className="sh-prompt">
      <span className="sh-caret" aria-hidden="true">›</span>
      <pre>{text}</pre>
    </div>
  )
}

function Reply({ m, expandAll }) {
  if (m.parts) return <div className="sh-reply"><MessageBody m={m} /></div>
  return (
    <div className="sh-reply">
      {m.activity?.length > 0 && <ActivityGroup parts={m.activity} expanded={expandAll} />}
      <Md text={m.content} />
      <ModelTag model={m.model} />
    </div>
  )
}

// /shell/p/:slug with no chat open: what the project is and where it stands,
// then its chats — not a dashboard. The composer below is already pinned here.
function ProjectHome({ slug, side, onOpen }) {
  const [proj, setProj] = useState(null)
  const [plan, setPlan] = useState(null)
  useEffect(() => {
    setProj(null)
    setPlan(null)
    api(`/api/projects/${encodeURIComponent(slug)}`).then(setProj).catch(() => setProj(false))
    api(`/api/projects/${encodeURIComponent(slug)}/plan`)
      .then((r) => setPlan(r.plan)).catch(() => {})
  }, [slug])
  if (proj === false) {
    return <div className="sh-home"><p className="dim">No project called “{slug}”.</p></div>
  }
  // the first real paragraph of project.md: skip headings and blank lines
  const lede = (proj?.project_md || '').split(/\n\s*\n/)
    .map((b) => b.trim()).find((b) => b && !b.startsWith('#')) || ''
  const items = plan?.items || []
  const done = items.filter((i) => i.status === 'done').length
  const chats = (side?.conversations || []).filter((c) => c.project_slug === slug)
  return (
    <div className="sh-home">
      <h1>{proj?.name || slug}</h1>
      {lede && <p className="sh-lede">{lede.length > 400 ? `${lede.slice(0, 400)}…` : lede}</p>}
      <div className="sh-home-meta">
        {items.length > 0 && <span>plan {done}/{items.length}</span>}
        <Link to={`/projects/${encodeURIComponent(slug)}`}>Open board</Link>
      </div>
      {chats.length > 0 && (
        <>
          <div className="sh-home-h">Chats</div>
          <ul className="sh-home-chats">
            {chats.slice(0, 8).map((c) => (
              <li key={c.id}>
                <button type="button" onClick={() => onOpen(c.id)}>
                  <span className="ellipsis">{listTitle(c)}</span>
                  <span className="dim">{c.running ? 'working' : ago(c.last_at || c.started_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}

export default function Transcript({
  cid, messages, expandAll, fresh, slug, side, agentName, temporary, onOpen, approvals,
  onReview,
}) {
  const box = useRef(null)
  const stick = useRef(true)

  const onScroll = () => {
    const el = box.current
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
  }
  // a different chat always opens at its end
  useEffect(() => { stick.current = true }, [cid, slug])
  useLayoutEffect(() => {
    const el = box.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [messages, approvals?.items])

  const empty = messages.length === 0
  const { after, tail } = placeApprovals(messages, approvals?.items || [])
  const approvalRows = (list) => list.map((a) => (
    <ApprovalRow key={a.key} a={a} acting={approvals.acting} onDecide={approvals.decide}
                 onReview={onReview} />
  ))
  return (
    <div className="sh-scroll" ref={box} onScroll={onScroll}>
      <div className="sh-thread">
        {empty && fresh && slug && <ProjectHome slug={slug} side={side} onOpen={onOpen} />}
        {empty && fresh && !slug && (
          <div className="sh-home">
            <h1>{temporary ? 'Temporary chat' : `New chat with ${agentName}`}</h1>
            <p className="sh-lede">
              {temporary
                ? 'Nothing from this chat is kept once you leave it.'
                : 'Not tied to a project — any files it makes stay with this chat.'}
            </p>
          </div>
        )}
        {messages.map((m, i) => {
          const row = m.role === 'user' ? <Prompt text={m.content} />
            : m.role === 'assistant' ? <Reply m={m} expandAll={expandAll} />
              : <div className="sh-error" role="alert">{m.content}</div>
          const waiting = after.get(i)
          return <Fragment key={i}>{row}{waiting && approvalRows(waiting)}</Fragment>
        })}
        {tail.length > 0 && approvalRows(tail)}
      </div>
    </div>
  )
}
