import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import Tabs from '../components/Tabs.jsx'
import Menu, { MenuItem } from '../components/Menu.jsx'
import EmptyState from '../components/EmptyState.jsx'
import PlanPanel from '../PlanPanel.jsx'
import { NetworkPanel } from '../pages/Network.jsx'
import OrganizerPanel from '../panels/OrganizerPanel.jsx'
import EditorPanel from '../panels/EditorPanel.jsx'
import RendererPanel from '../panels/RendererPanel.jsx'
import GitPanel from '../panels/GitPanel.jsx'
import TerminalPanel from '../panels/TerminalPanel.jsx'
import TodoPanel from '../panels/TodoPanel.jsx'
import JournalPanel from '../panels/JournalPanel.jsx'
import ContextPanel from '../panels/ContextPanel.jsx'
import GrantsPanel from '../panels/GrantsPanel.jsx'
import RunsTab from './RunsTab.jsx'
import { useIsPhone } from '../breakpoints.js'

// The panel dock: the Workspace board's panels as tabs beside the transcript,
// one visible at a time. The components are the board's own (panels/*), so
// nothing is maintained twice.
//
// Scope decides what exists. A project chat docks onto the project; a
// project-less chat onto its own hidden store (`chat-<id>`, created the first
// time a file tool runs), which has files, to-dos and a network policy but no
// git, plan, terminal, journal or context — those tabs are hidden, not
// disabled. A brand-new chat has no scope yet.

export const TABS = [
  { id: 'files', label: 'Files' },
  { id: 'git', label: 'Git', project: true },
  { id: 'plan', label: 'Plan', project: true },
  { id: 'term', label: 'Term', project: true },
  { id: 'net', label: 'Net' },
  { id: 'runs', label: 'Runs' },
]
export const MORE = [
  { id: 'todos', label: 'To-dos' },
  { id: 'journal', label: 'Journal', project: true },
  { id: 'context', label: 'Context files', project: true },
  { id: 'secrets', label: 'Secrets', project: true },
]
export const tabsFor = (isProject) =>
  [...TABS, ...MORE].filter((t) => isProject || !t.project).map((t) => t.id)

const FILE_VIEWS = [
  { id: 'browse', label: 'Browse' }, { id: 'edit', label: 'Edit' },
  { id: 'preview', label: 'Preview' },
]
const MIN_W = 360
const MAX_W = 760

// Files: the organizer, the editor and the renderer folded into one tab.
function FilesTab({ slug }) {
  const [view, setView] = useState('browse')
  const [edit, setEdit] = useState({})
  const [prev, setPrev] = useState({})
  useEffect(() => { setEdit({}); setPrev({}) }, [slug])
  return (
    <div className="pane-col dock-files">
      <Tabs items={FILE_VIEWS} value={view} onChange={setView} label="files view"
            className="dock-subtabs" />
      {view === 'browse' && <OrganizerPanel slug={slug} />}
      {view === 'edit' && (
        <EditorPanel slug={slug} state={edit} setState={(p) => setEdit((s) => ({ ...s, ...p }))} />
      )}
      {view === 'preview' && (
        <RendererPanel slug={slug} state={prev} setState={(p) => setPrev((s) => ({ ...s, ...p }))} />
      )}
    </div>
  )
}

// the journal panel edits project.md and wants the project row it came from
function Journal({ slug }) {
  const [project, setProject] = useState(null)
  const refresh = useCallback(
    () => api(`/api/projects/${encodeURIComponent(slug)}`).then(setProject).catch(() => {}),
    [slug])
  useEffect(() => { refresh() }, [refresh])
  if (!project) return null
  return <JournalPanel key={slug} slug={slug} project={project} refreshProject={refresh} />
}

function Body({ tab, slug, isProject, chatJobs, plan, setPlan }) {
  switch (tab) {
    case 'files': return <FilesTab slug={slug} />
    case 'git': return <GitPanel slug={slug} />
    case 'plan': return <PlanPanel slug={slug} state={plan}
                                   setState={(p) => setPlan((s) => ({ ...s, ...p }))} />
    case 'term': return <TerminalPanel slug={slug} />
    case 'net': return <NetworkPanel slug={slug} />
    case 'runs': return <RunsTab project={isProject ? slug : null} chatJobs={chatJobs} />
    case 'todos': return <TodoPanel slug={slug} />
    case 'journal': return <Journal slug={slug} />
    case 'context': return <ContextPanel slug={slug} />
    case 'secrets': return <GrantsPanel slug={slug} />
    default: return null
  }
}

export default function Dock({
  slug, isProject, chatJobs, tab, onTab, onClose, width, onWidth,
}) {
  const [more, setMore] = useState(false)
  const [exists, setExists] = useState(null)      // a chat store may not exist yet
  const [plan, setPlan] = useState({})
  const closeMore = useCallback(() => setMore(false), [])
  const drag = useRef(null)

  useEffect(() => {
    setPlan({})
    if (!slug) { setExists(false); return }
    if (isProject) { setExists(true); return }
    setExists(null)
    api(`/api/projects/${encodeURIComponent(slug)}`)
      .then(() => setExists(true)).catch(() => setExists(false))
  }, [slug, isProject])

  // drag the dock's left edge; the width is the caller's to keep
  function startDrag(e) {
    e.preventDefault()
    drag.current = { x: e.clientX, w: width }
    const move = (ev) => {
      const d = drag.current
      if (d) onWidth(Math.max(MIN_W, Math.min(MAX_W, d.w + (d.x - ev.clientX))))
    }
    const up = () => {
      drag.current = null
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const phone = useIsPhone()
  const allowed = tabsFor(isProject)
  const shown = TABS.filter((t) => allowed.includes(t.id))
  const overflow = MORE.filter((t) => allowed.includes(t.id))
  const inMore = overflow.find((t) => t.id === tab)
  const needsStore = !isProject && tab !== 'runs'

  return (
    <aside className="shell-dock" style={{ '--dock-w': `${width}px` }} aria-label="panels">
      <div className="dock-grip" role="separator" aria-orientation="vertical"
           aria-label="resize panels" onPointerDown={startDrag} />
      <div className="dock-head">
        {phone ? (
          // a phone can't fit six tabs and the overflow in one row: every
          // panel goes in one glass menu, named by the one that is open
          <Menu open={more} onClose={closeMore} floating align="left" width={220} label="panels"
                trigger={(
                  <button type="button" className="sh-chip dock-more on" aria-haspopup="menu"
                          aria-expanded={more} onClick={() => setMore((o) => !o)}>
                    {[...shown, ...overflow].find((t) => t.id === tab)?.label || 'Panels'}
                    <span className={more ? 'chev open' : 'chev'} aria-hidden="true">›</span>
                  </button>
                )}>
            {[...shown, ...overflow].map((t) => (
              <MenuItem key={t.id} checked={tab === t.id}
                        onClick={() => { setMore(false); onTab(t.id) }}>{t.label}</MenuItem>
            ))}
          </Menu>
        ) : <>
        <Tabs items={shown.map((t) => ({ id: t.id, label: t.label }))}
              value={inMore ? null : tab} onChange={onTab} label="panels"
              className="dock-tabs" />
        {overflow.length > 0 && (
          <Menu open={more} onClose={closeMore} floating width={200} label="more panels"
                trigger={(
                  <button type="button" className={`sh-chip dock-more${inMore ? ' on' : ''}`}
                          aria-haspopup="menu" aria-expanded={more}
                          onClick={() => setMore((o) => !o)}>
                    {inMore ? inMore.label : 'More'}
                    <span className={more ? 'chev open' : 'chev'} aria-hidden="true">›</span>
                  </button>
                )}>
            {overflow.map((t) => (
              <MenuItem key={t.id} checked={tab === t.id}
                        onClick={() => { setMore(false); onTab(t.id) }}>{t.label}</MenuItem>
            ))}
          </Menu>
        )}
        </>}
        <span className="grow" />
        {isProject && (
          <Link className="dock-board" to={`/projects/${encodeURIComponent(slug)}`}
                title="the full Workspace board for this project">Board</Link>
        )}
        <button type="button" className="icon-btn" aria-label="close panels"
                title="close panels" onClick={onClose}>✕</button>
      </div>
      <div className="window-body dock-body">
        {!slug
          ? <EmptyState pad>Panels open onto this chat’s files once it has started —
              or pick a project from the chip above.</EmptyState>
          : needsStore && exists === false
            ? <EmptyState pad>This chat has no files yet. Anything it writes shows up here.</EmptyState>
            : exists && <Body key={`${slug}:${tab}`} tab={tab} slug={slug} isProject={isProject}
                              chatJobs={chatJobs} plan={plan} setPlan={setPlan} />}
      </div>
    </aside>
  )
}
