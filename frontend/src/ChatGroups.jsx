import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api.js'
import { useAsk } from './ask.jsx'
import { notifyError } from './notify.js'
import Menu, { MenuItem, MenuSep } from './components/Menu.jsx'
import Input from './components/Input.jsx'
import Button from './components/Button.jsx'

// The chat sidebar's list, grouped: Projects (links to their workspace), then
// Starred, then each folder, then Recent (everything unfiled). Each chat shows
// exactly once — a starred chat lives under Starred even if it is filed, and
// returns to its folder when unstarred.
//
// Self-contained over the folder/star API (GET/POST/PATCH/DELETE
// /api/chat/folders, PATCH /api/conversations/{id} {starred, folder_id}) so a
// different chat shell can mount it as-is: the caller owns the conversation
// list and how a chat opens, renames and deletes; this owns folders, stars and
// which groups are folded.

const GROUPS_KEY = 'jarvis.chat.groups'

function readFolded() {
  try { return JSON.parse(localStorage.getItem(GROUPS_KEY)) || {} } catch { return {} }
}

// A chat's line in the sidebar. Untitled chats are summarised from the first
// prompt, so a pasted brief arrives as "# AGENT BRIEF…" or "[startup — …":
// the markdown heading mark and the bracket are noise in a list, and they
// are stripped here only — the toolbar and the tooltip keep the real title.
export function listTitle(c) {
  if (!c.summary) return `#${c.id} · ${c.started_at?.slice(5, 16) || ''}`
  return c.summary.replace(/^[#[\s]+/, '') || c.summary
}

function GroupHead({ id, label, count, folded, onToggle, children }) {
  return (
    <div className="convo-group-head">
      <button type="button" className="convo-group-toggle" aria-expanded={!folded}
              aria-controls={`convo-group-${id}`} onClick={() => onToggle(id)}>
        <span className={folded ? 'chev' : 'chev open'} aria-hidden="true">›</span>
        <span className="ellipsis">{label}</span>
        {count > 0 && <span className="convo-group-count">{count}</span>}
      </button>
      {children}
    </div>
  )
}

export default function ChatGroups({
  conversations, activeId, projects = [], onOpen, onRename, onDelete, onChanged,
}) {
  const ask = useAsk()
  const [folders, setFolders] = useState([])
  const [folded, setFolded] = useState(readFolded)
  const [menu, setMenu] = useState(null)        // 'c:<id>' | 'f:<id>' | null
  const [draft, setDraft] = useState(null)      // new-folder name while typing
  const closeMenu = useCallback(() => setMenu(null), [])

  const loadFolders = useCallback(
    () => api('/api/chat/folders').then((r) => setFolders(r.folders)).catch(() => {}),
    [])
  useEffect(() => { loadFolders() }, [loadFolders])

  useEffect(() => {
    try { localStorage.setItem(GROUPS_KEY, JSON.stringify(folded)) } catch { /* private mode */ }
  }, [folded])
  const toggle = (id) => setFolded((f) => ({ ...f, [id]: !f[id] }))

  async function patchChat(id, body) {
    setMenu(null)
    try {
      await api(`/api/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
    } catch (err) { notifyError(err) }
    onChanged?.()
    loadFolders()
  }

  async function createFolder(name, thenFile = null) {
    const clean = name.trim()
    if (!clean) return null
    try {
      const r = await api('/api/chat/folders', {
        method: 'POST', body: JSON.stringify({ name: clean }) })
      await loadFolders()
      if (thenFile != null) await patchChat(thenFile, { folder_id: r.folder.id })
      return r.folder
    } catch (err) {
      notifyError(err)
      return null
    }
  }

  async function renameFolder(f) {
    setMenu(null)
    const next = await ask.prompt('Rename folder', f.name, { confirmLabel: 'Rename' })
    if (!next?.trim()) return
    try {
      await api(`/api/chat/folders/${f.id}`, {
        method: 'PATCH', body: JSON.stringify({ name: next.trim() }) })
    } catch (err) { notifyError(err) }
    loadFolders()
  }

  async function deleteFolder(f) {
    setMenu(null)
    if (!await ask.confirm(`Delete the folder “${f.name}”? Its chats move to Recent.`,
                           { confirmLabel: 'Delete', danger: true })) return
    try {
      await api(`/api/chat/folders/${f.id}`, { method: 'DELETE' })
    } catch (err) { notifyError(err) }
    loadFolders()
    onChanged?.()
  }

  async function moveToNewFolder(c) {
    setMenu(null)
    const name = await ask.prompt('New folder', '', { confirmLabel: 'Create' })
    if (name?.trim()) await createFolder(name, c.id)
  }

  const known = new Set(folders.map((f) => f.id))
  const starred = conversations.filter((c) => c.starred)
  const inFolder = (fid) => conversations.filter((c) => !c.starred && c.folder_id === fid)
  // a folder id the list doesn't know yet (created elsewhere) reads as unfiled
  // rather than vanishing until the next folder refresh
  const recent = conversations.filter(
    (c) => !c.starred && (c.folder_id == null || !known.has(c.folder_id)))

  const row = (c) => {
    const key = `c:${c.id}`
    return (
      <li key={c.id}
          className={[c.id === activeId ? 'active' : '', menu === key ? 'menu-open' : '']
            .filter(Boolean).join(' ') || undefined}
          onClick={() => onOpen(c.id)}>
        {/* title owns the row; the project slug sits under it so a long slug
            can never crush the title into two letters */}
        <div className="convo-main">
          <span className="convo-title ellipsis" title={c.summary || `#${c.id}`}>
            {listTitle(c)}</span>
          {/* an agent's thread is still a chat and still belongs in this list —
              it just isn't Jav3 speaking, so say so */}
          {(c.agent_slug || c.project_slug) && (
            <span className="convo-proj ellipsis">
              {c.agent_slug && <span className="convo-agent">{c.agent_slug}</span>}
              {c.project_slug}
            </span>
          )}
        </div>
        {/* overlaid on hover rather than reserving width. The menu is portalled
            (the list scrolls and the sidebar clips), but React still bubbles
            its clicks through here — stop them before they open the row. */}
        <span className="convo-actions" onClick={(e) => e.stopPropagation()}>
          <Menu open={menu === key} onClose={closeMenu} floating align="left"
                label="chat actions" width={220}
                trigger={
                  <button type="button" className="win-btn" title="chat actions"
                          aria-haspopup="menu" aria-expanded={menu === key}
                          onClick={() => setMenu(menu === key ? null : key)}>⋯</button>
                }>
            <MenuItem onClick={() => patchChat(c.id, { starred: !c.starred })}>
              {c.starred ? 'Unstar' : 'Star'}</MenuItem>
            <MenuItem onClick={() => { setMenu(null); onRename(c) }}>Rename</MenuItem>
            <MenuSep />
            <div className="menu-caption">Move to folder</div>
            <MenuItem checked={c.folder_id == null || !known.has(c.folder_id)}
                      onClick={() => patchChat(c.id, { folder_id: null })}>
              No folder</MenuItem>
            {folders.map((f) => (
              <MenuItem key={f.id} checked={c.folder_id === f.id}
                        onClick={() => patchChat(c.id, { folder_id: f.id })}>
                <span className="ellipsis">{f.name}</span></MenuItem>
            ))}
            <MenuItem onClick={() => moveToNewFolder(c)}>New folder…</MenuItem>
            <MenuSep />
            <MenuItem danger onClick={() => { setMenu(null); onDelete(c) }}>Delete</MenuItem>
          </Menu>
        </span>
      </li>
    )
  }

  const group = (id, label, rows, { head = null, empty = null } = {}) => (
    <section key={id} className="convo-group">
      <GroupHead id={id} label={label} count={rows.length} folded={!!folded[id]}
                 onToggle={toggle}>{head}</GroupHead>
      {!folded[id] && (rows.length
        ? <ul id={`convo-group-${id}`} className="convo-rows">{rows.map(row)}</ul>
        : empty && <p id={`convo-group-${id}`} className="convo-group-empty">{empty}</p>)}
    </section>
  )

  return (
    <div className="convo-list">
      {projects.length > 0 && (
        <section className="convo-group">
          <GroupHead id="projects" label="Projects" count={projects.length}
                     folded={!!folded.projects} onToggle={toggle} />
          {!folded.projects && (
            <ul id="convo-group-projects" className="convo-rows proj-rows">
              {projects.map((p) => (
                <li key={p.slug}>
                  <Link to={`/projects/${encodeURIComponent(p.slug)}`} title={p.slug}>
                    <span className="proj-dot" aria-hidden="true" />
                    <span className="convo-title ellipsis">{p.name}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {starred.length > 0 && group('starred', 'Starred', starred)}
      {folders.map((f) => {
        const key = `f:${f.id}`
        return group(key, f.name, inFolder(f.id), {
          empty: 'Empty — file a chat here from its ⋯ menu.',
          head: (
            <Menu open={menu === key} onClose={closeMenu} floating label="folder actions"
                  width={180} wrapClassName="convo-group-menu"
                  trigger={
                    <button type="button" className="win-btn" title="folder actions"
                            aria-haspopup="menu" aria-expanded={menu === key}
                            onClick={() => setMenu(menu === key ? null : key)}>⋯</button>
                  }>
              <MenuItem onClick={() => renameFolder(f)}>Rename</MenuItem>
              <MenuItem danger onClick={() => deleteFolder(f)}>Delete folder</MenuItem>
            </Menu>
          ),
        })
      })}
      {draft === null
        ? <Button variant="ghost" className="convo-new-folder"
                  onClick={() => setDraft('')}>+ folder</Button>
        : (
          <form className="convo-new-folder-form"
                onSubmit={async (e) => {
                  e.preventDefault()
                  if (await createFolder(draft)) setDraft(null)
                }}>
            <Input autoFocus value={draft} placeholder="Folder name" maxLength={60}
                   aria-label="new folder name"
                   onChange={(e) => setDraft(e.target.value)}
                   onKeyDown={(e) => { if (e.key === 'Escape') setDraft(null) }}
                   onBlur={() => { if (!draft.trim()) setDraft(null) }} />
          </form>
        )}
      {group('recent', 'Recent', recent, { empty: 'No chats yet.' })}
    </div>
  )
}
