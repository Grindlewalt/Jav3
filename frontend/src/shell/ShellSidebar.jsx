import { useState } from 'react'
import { api } from '../api.js'
import ChatGroups, { listTitle } from '../ChatGroups.jsx'
import SandboxLine from './SandboxLine.jsx'

// The shell's left column, top to bottom:
//
//   Needs you   chats whose scope has something waiting on the operator — a
//               git commit request, a network host, a plan item that stopped
//               to ask (GET /api/sidebar `needs`)
//   Working     every other chat with a live turn, any project, Jav3 or agent
//   …           ChatGroups: Projects, Starred, folders, Recent — the same
//               component the Chat page mounts, fed from the same response
//   foot        the sandbox line
//
// The two live sections only render when non-empty: an empty "Working" header
// is noise on the screen you look at most.

function what(n) {
  const bits = []
  if (n.egress) bits.push(`${n.egress} site${n.egress > 1 ? 's' : ''}`)
  if (n.git) bits.push(`${n.git} commit${n.git > 1 ? 's' : ''}`)
  if (n.plan) bits.push(`${n.plan} plan question${n.plan > 1 ? 's' : ''}`)
  return bits.join(' · ')
}

function LiveSection({ label, children }) {
  return (
    <section className="sh-live">
      <div className="sh-live-h">{label}</div>
      <ul className="sh-live-rows">{children}</ul>
    </section>
  )
}

export default function ShellSidebar({
  side, activeId, activeSlug, agentName, onOpen, onOpenProject, onNew, onRename,
  onDelete, onChanged, onClose,
}) {
  const [stopping, setStopping] = useState(null)
  const needs = side?.needs || []
  const needIds = new Set(needs.map((n) => n.conversation_id).filter(Boolean))
  const working = (side?.running || []).filter((c) => !needIds.has(c.id))

  async function stop(e, id) {
    e.stopPropagation()
    setStopping(id)
    try { await api(`/api/chat/${id}/stop`, { method: 'POST' }) } catch { /* already done */ }
    setStopping(null)
    onChanged()
  }

  const openNeed = (n) => {
    if (n.conversation_id) onOpen(n.conversation_id)
    else if (n.project) onOpenProject(n.project)
  }

  return (
    <aside className="shell-side" aria-label="chats">
      <div className="sh-side-head">
        <span className="sh-side-title">Chats</span>
        <button type="button" className="icon-btn" title="new chat" aria-label="new chat"
                onClick={onNew}>＋</button>
        <button type="button" className="icon-btn sh-side-close" title="close"
                aria-label="close chats" onClick={onClose}>✕</button>
      </div>
      <div className="sh-side-scroll">
        {needs.length > 0 && (
          <LiveSection label="Needs you">
            {needs.map((n) => (
              <li key={n.scope}
                  className={n.conversation_id === activeId
                             || (!n.conversation_id && n.project === activeSlug)
                    ? 'active' : undefined}>
                <button type="button" onClick={() => openNeed(n)} title={n.title}>
                  <span className="sh-dot amber" aria-hidden="true" />
                  <span className="sh-live-main">
                    <span className="ellipsis">{n.agent_slug && <span className="convo-agent">@{n.agent_slug}</span>}
                      {n.title}</span>
                    <span className="sh-live-sub ellipsis">{what(n)}</span>
                  </span>
                </button>
              </li>
            ))}
          </LiveSection>
        )}
        {working.length > 0 && (
          <LiveSection label="Working">
            {working.map((c) => (
              <li key={c.id} className={c.id === activeId ? 'active' : undefined}>
                <button type="button" onClick={() => onOpen(c.id)} title={c.summary || ''}>
                  <span className="sh-dot live" aria-hidden="true" />
                  <span className="sh-live-main">
                    <span className="ellipsis">
                      {c.agent_slug && <span className="convo-agent"
                                             title={agentName(c.agent_slug)}>@{c.agent_slug}</span>}
                      {listTitle(c)}</span>
                    {c.project_slug && <span className="sh-live-sub ellipsis">{c.project_slug}</span>}
                  </span>
                </button>
                <button type="button" className="win-btn sh-stop" title="stop this turn"
                        aria-label={`stop ${listTitle(c)}`} disabled={stopping === c.id}
                        onClick={(e) => stop(e, c.id)}>◼</button>
              </li>
            ))}
          </LiveSection>
        )}
        {side && (
          <ChatGroups conversations={side.conversations} folders={side.folders}
                      projects={side.projects} activeId={activeId}
                      projectHref={(p) => `/shell/p/${encodeURIComponent(p.slug)}`}
                      onOpen={onOpen} onRename={onRename} onDelete={onDelete}
                      onChanged={onChanged} />
        )}
      </div>
      <div className="sh-side-foot"><SandboxLine /></div>
    </aside>
  )
}
