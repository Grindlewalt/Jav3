import {
  Suspense, lazy, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useIsPhone } from '../breakpoints.js'
import { useDismiss } from '../useDismiss.js'
import ErrorBoundary from '../ErrorBoundary.jsx'
import Chat from '../pages/Chat.jsx'
import { WorkContext } from './context.js'
import {
  MAX_PANELS, addCard, canAdd, closeCard, cycleFocus, findLeaf, findSplit, focusToward,
  fromSaved, geometry, leaves, minimizeCard, moveCard, patchCardState, resize, restoreCard,
  switchCard, toSaved,
} from './layout.js'
import { ALIASES, WINDOW_TYPES, isWindowType } from './types.js'

// the cards (xterm, the editors, …) and the Projects page load on first use,
// so the chat does not wait for them
const WindowBody = lazy(() => import('./windows.jsx'))
const Projects = lazy(() => import('../pages/Projects.jsx'))

// Work: the chat (pages/Chat.jsx, unchanged in look and behaviour) with the
// chat's project's windows beside it — the old project board's cards as a
// split tree (layout.js). The chat is the main area and cannot be closed; the
// windows share the area to its right, and when the last one closes the chat
// has the full width again.
//
// The windows belong to a PROJECT, not to the chat: they are the project's
// board, saved server-side (GET/PUT /api/projects/{slug}/layout, so they
// follow the operator across devices), and they swap when the chat's project
// does. The chat|windows split width is a per-device preference (localStorage).
//
// Phone (breakpoints.js): no splits. A strip of tabs — Chat and each open
// window — shows one at a time, full width, with the + at its end.

const MIN_W = 220        // px: the narrowest a window can be dragged
const MIN_H = 140
const CHAT_MIN = 360     // px the chat keeps when the windows are dragged wide
const WIN_MIN = 280      // px the windows area keeps
const SPLIT_KEY = 'jav3.work.split'
const known = (t) => isWindowType(t)
const pct = (v) => `${(v * 100).toFixed(4)}%`
const ARROWS = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' }
const NOT_TEXT = new Set(['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file'])
// is the keyboard busy typing somewhere (so Tab and the arrows are the field's)?
function typing(el) {
  if (!el || el === document.body) return false
  if (el.isContentEditable || el.closest?.('.xterm, .cm-editor, [role="dialog"], [role="menu"]')) return true
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true
  return el.tagName === 'INPUT' && !NOT_TEXT.has((el.type || '').toLowerCase())
}

function readSplit() {
  try {
    const v = Number(localStorage.getItem(SPLIT_KEY))
    if (v > 0.1 && v < 0.9) return v
  } catch { /* private mode */ }
  return 0.5
}

export default function Work({ openProjects = false }) {
  const { slug: routeSlug } = useParams()      // /projects/:slug lands here
  const navigate = useNavigate()
  const phone = useIsPhone()
  const chatCtl = useRef(null)
  const [project, setProject] = useState(null)       // the chat's project slug
  const [projectObj, setProjectObj] = useState(null)  // GET /api/projects/{slug}
  const [projects, setProjects] = useState([])
  const [board, setBoard] = useState(null)           // { slug, root, focus, maximized, panels }
  const [picker, setPicker] = useState(null)         // { mode, target, dir, anchor }
  const [sheet, setSheet] = useState(false)          // the Projects… sheet
  const [split, setSplit] = useState(readSplit)      // windows' share of chat+windows
  const [tab, setTab] = useState('chat')             // phone: 'chat' or a window id
  const boardRef = useRef(board)
  boardRef.current = board
  const projectRef = useRef(project)
  projectRef.current = project
  const skipSave = useRef(true)
  const saveTimer = useRef(null)
  const layoutRef = useRef(null)                     // .chat-layout's main + windows

  // ---- a /projects/:slug link: into Work, with that project on the chat ----
  useEffect(() => {
    if (!routeSlug) return
    // Chat's control ref is filled on its first render, which precedes this
    chatCtl.current?.openProject(routeSlug)
    navigate('/', { replace: true })
  }, [routeSlug, navigate])
  // /projects (the old Projects page): the Projects sheet, over Work
  useEffect(() => {
    if (!openProjects) return
    setSheet(true)
    navigate('/', { replace: true })
  }, [openProjects, navigate])

  const reloadProjects = useCallback(() =>
    api('/api/projects').then((r) => setProjects(r.projects || [])).catch(() => {}), [])
  useEffect(() => { reloadProjects() }, [reloadProjects])

  const onProjectChange = useCallback((slug) => setProject(slug || null), [])

  // ---- the project's board ----
  const loadBoard = useCallback((slug) => {
    clearTimeout(saveTimer.current)
    return api(`/api/projects/${encodeURIComponent(slug)}/layout`).then((r) => {
      // the chat may have moved on while this was in flight: a board must
      // never land on (and then be saved into) another project
      if (projectRef.current !== slug) return
      const { board: b, converted } = fromSaved(r.layout, known, { legacyDrop: ['chat'] })
      skipSave.current = !converted       // a converted old board saves as v2 once
      // maximize is a moment, not a layout: a board never opens maximized
      // (a saved one used to come back with the chat gone and every other
      // window hidden, which read as a broken board)
      setBoard({ ...b, maximized: null, slug })
    }).catch(() => {
      if (projectRef.current !== slug) return
      skipSave.current = true
      setBoard(null)
    })
  }, [])

  const refreshProject = useCallback(() => {
    const slug = projectRef.current
    if (!slug) return Promise.resolve()
    return api(`/api/projects/${encodeURIComponent(slug)}`).then((p) => {
      if (projectRef.current === slug) setProjectObj(p)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    // reset first: the old project's board is gone before anything else runs
    clearTimeout(saveTimer.current)
    setBoard(null)
    setProjectObj(null)
    setTab('chat')
    if (!project) return
    loadBoard(project)
    refreshProject()
  }, [project, loadBoard, refreshProject])

  // Jav3 rearranged the board server-side (the workspace_panel tool)
  useEffect(() => {
    const h = (e) => {
      const slug = projectRef.current
      if (slug && (!e.detail?.slug || e.detail.slug === slug)) loadBoard(slug)
    }
    window.addEventListener('jarvis-layout-changed', h)
    return () => window.removeEventListener('jarvis-layout-changed', h)
  }, [loadBoard])

  // debounced persistence, to the project the board was loaded for
  useEffect(() => {
    if (!board) return undefined
    if (skipSave.current) { skipSave.current = false; return undefined }
    clearTimeout(saveTimer.current)
    const { slug } = board
    saveTimer.current = setTimeout(() => {
      if (projectRef.current !== slug) return
      api(`/api/projects/${encodeURIComponent(slug)}/layout`, {
        method: 'PUT', body: JSON.stringify(toSaved(board)) }).catch(() => {})
    }, 700)
    return () => clearTimeout(saveTimer.current)
  }, [board])

  useEffect(() => {
    try { localStorage.setItem(SPLIT_KEY, String(split)) } catch { /* private mode */ }
  }, [split])

  // ---- board actions ----
  const update = useCallback((fn) => {
    const b = boardRef.current
    if (!b) return null
    const next = fn(b)
    boardRef.current = next
    setBoard(next)
    return next
  }, [])

  const focusWin = useCallback((id) => {
    update((b) => (b.focus === id ? b : { ...b, focus: id }))
  }, [update])
  // no id: the focused window (and nothing, when none is)
  const closeWindow = useCallback((id0) => {
    const id = id0 ?? boardRef.current?.focus
    if (!id) return
    update((b) => closeCard(b, id))
    setTab((t) => (t === id ? 'chat' : t))
  }, [update])
  const toggleMax = useCallback((id) => {
    update((b) => ({ ...b, focus: id, maximized: b.maximized === id ? null : id }))
  }, [update])
  const minimize = useCallback((id) => { update((b) => minimizeCard(b, id)) }, [update])
  const restore = useCallback((id) => { update((b) => restoreCard(b, id)) }, [update])
  const setCardState = useCallback((id, patch) => {
    update((b) => patchCardState(b, id, patch))
  }, [update])

  const openWindow = useCallback((type0, props) => {
    const type = ALIASES[type0] || type0
    if (!isWindowType(type) || !boardRef.current) return null
    const b0 = boardRef.current
    const hit = b0.panels.find((p) => p.type === type && !p.project)
    if (!hit && !canAdd(b0)) return null            // MAX_PANELS open already
    // `slug` is not card state: a window is always on the chat's project
    const { slug: _slug, ...patch } = props || {}
    const next = update((b) => {
      let n = hit
        ? { ...restoreCard(b, hit.id), maximized: b.maximized && b.maximized !== hit.id ? null : b.maximized }
        : addCard(b, type, b.focus, 'row')
      if (Object.keys(patch).length) n = patchCardState(n, n.focus, patch)
      return n
    })
    if (next && phone) setTab(next.focus)
    return next?.focus ?? null
  }, [update, phone])

  const focused = board?.focus ?? null
  const workCtx = useMemo(() => ({
    available: true, project, focused, openWindow, closeWindow,
  }), [project, focused, openWindow, closeWindow])

  // ---- the + menu ----
  const openPicker = useCallback((e, mode, target, dir = 'row') => {
    const r = e?.currentTarget?.getBoundingClientRect?.()
    setPicker((p) => (p && p.mode === mode && p.target === target && p.dir === dir ? null
      : { mode, target, dir, anchor: r ? { left: r.left, right: r.right, bottom: r.bottom } : null }))
  }, [])
  // `extra`: { project } for another project's window, { state } to start
  // it with (a chat window's conversation)
  const onPick = (type, extra = {}) => {
    const p = picker
    if (!p || !boardRef.current) return
    const next = p.mode === 'switch'
      ? update((b) => switchCard(b, p.target, type, extra))
      : update((b) => addCard(b, type, p.target || b.focus, p.dir, extra))
    if (next && phone) setTab(next.focus)
  }

  // ctrl/cmd+\ : split the focused window to the right (the + menu picks what
  // goes in the new half); with no window open, the first one beside the chat
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.key !== '\\') return
      e.preventDefault()
      const b = boardRef.current
      setPicker({ mode: 'add', target: b?.focus || null, dir: 'row', anchor: null })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Panels from the keyboard, when it is not busy in a text field:
  //   Tab / Shift+Tab (tap)   focus the next / previous window
  //   hold Tab + arrow        focus the window that way
  //   Shift+← / Shift+→       swap the focused window with its neighbour
  //   Escape                  un-maximize
  useEffect(() => {
    let held = null                                  // { used, shift } while Tab is down
    const busy = () => typing(document.activeElement)
      || !!document.querySelector('.work-sheet-scrim, .work-picker')
    const onDown = (e) => {
      const b = boardRef.current
      if (!b?.root || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return
      if (e.key === 'Tab') {
        if (!held && (e.repeat || busy())) return
        e.preventDefault()
        if (!held) held = { used: false, shift: e.shiftKey }
        return
      }
      const dir = ARROWS[e.key]
      if (dir && held) {
        e.preventDefault()
        held.used = true
        update((x) => focusToward(x, dir))
        return
      }
      if (busy()) return
      if (e.shiftKey && (dir === 'left' || dir === 'right')) {
        e.preventDefault()
        update((x) => moveCard(x, dir))
      } else if (e.key === 'Escape' && b.maximized) {
        e.preventDefault()
        update((x) => ({ ...x, maximized: null }))
      }
    }
    const onUp = (e) => {
      if (e.key !== 'Tab' || !held) return
      const h = held
      held = null
      if (!h.used) update((x) => cycleFocus(x, h.shift ? -1 : 1))
    }
    const reset = () => { held = null }
    window.addEventListener('keydown', onDown)
    window.addEventListener('keyup', onUp)
    window.addEventListener('blur', reset)
    return () => {
      window.removeEventListener('keydown', onDown)
      window.removeEventListener('keyup', onUp)
      window.removeEventListener('blur', reset)
    }
  }, [update])

  // ---- the chat | windows divider ----
  const onChatDivider = (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    const host = layoutRef.current?.closest('.chat-layout')
    const main = host?.querySelector(':scope > main')
    const wins = layoutRef.current
    if (!main || !wins) return
    const right = wins.getBoundingClientRect().right
    const total = right - main.getBoundingClientRect().left
    const el = e.currentTarget
    el.setPointerCapture?.(e.pointerId)
    document.body.classList.add('work-dragging-col')
    const move = (ev) => {
      const w = Math.max(WIN_MIN, Math.min(total - CHAT_MIN, right - ev.clientX))
      setSplit(Math.max(0.1, Math.min(0.9, w / total)))
    }
    const up = () => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      document.body.classList.remove('work-dragging-col')
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }

  // ---- render ----
  const all = leaves(board?.root)
  const minimized = board?.minimized || EMPTY_LIST
  const geo = useMemo(() => geometry(board?.root, minimized), [board?.root, minimized])
  const minLeaves = phone ? [] : all.filter((l) => minimized.includes(l.id))
  const stageRef = useRef(null)
  const phoneWin = phone && tab !== 'chat' && all.some((l) => l.id === tab)
  const showWindows = phone ? phoneWin : all.length > 0
  // maximized: that window has the whole Work area, chat included (.work-max
  // hides <main>); the others keep their place, invisible, so a terminal or an
  // iframe is never squeezed to 0x0 and back
  const maxId = !phone && board?.maximized && all.some((l) => l.id === board.maximized)
    ? board.maximized : null
  const single = phone || !!maxId
  const shownId = phone ? tab : maxId
  const panelOf = (id) => board?.panels.find((p) => p.id === id)
  const stateOf = (id) => panelOf(id)?.state
  // what a header says after the card's title: whose it is, when not the chat's
  const subOf = (leaf) => {
    const p = panelOf(leaf.id)
    if (leaf.type === 'chat' && p?.state && 'chatProject' in p.state) {
      const c = p.state.chatProject
      return c === null ? 'no project' : c !== board.slug ? c : ''
    }
    return p?.project && p.project !== board.slug ? p.project : ''
  }

  const plus = (
    <button type="button" className={`work-plus${picker?.mode === 'add' ? ' on' : ''}`}
            title={project ? 'open a window (ctrl+\\)' : 'windows: pick a project for this chat first'}
            aria-label="open a window" aria-expanded={picker?.mode === 'add'}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => openPicker(e, 'add', board?.focus || null)}>+</button>
  )

  const beside = (
    <div ref={layoutRef}
         className={`work-windows${showWindows ? '' : ' hidden'}${phone ? ' phone' : ''}`}
         style={phone || maxId ? undefined : { flexBasis: pct(split) }}>
      {!phone && !maxId && <div className="work-div-chat" role="separator" aria-orientation="vertical"
                      title="drag to resize" onPointerDown={onChatDivider}
                      onDoubleClick={() => setSplit(0.5)} />}
      {minLeaves.length > 0 && (
        <div className="work-minbar" aria-label="minimized windows">
          {minLeaves.map((l) => (
            <button key={l.id} type="button" className="work-mintab" title="restore"
                    onClick={() => restore(l.id)}>
              {WINDOW_TYPES[l.type]?.title || l.type}</button>
          ))}
        </div>
      )}
      <div className="work-stage" ref={stageRef}>
        {board && projectObj && all.map((leaf) => {
          const r = geo.leaves[leaf.id]
          const min = !phone && !r
          const hidden = min || (single && leaf.id !== shownId)
          const full = single && !hidden
          const place = hidden && !phone && !min ? r : null
          return (
            <WindowFrame key={leaf.id} leaf={leaf} slug={board.slug}
                         other={leaf.type === 'chat' ? null : (panelOf(leaf.id)?.project || null)}
                         sub={subOf(leaf)}
                         x={full ? 0 : r?.x} y={full ? 0 : r?.y}
                         w={full ? 1 : r?.w} h={full ? 1 : r?.h}
                         hidden={hidden} keep={!!place} phone={phone}
                         maximized={board.maximized === leaf.id}
                         focused={board.focus === leaf.id}
                         project={projectObj} refreshProject={refreshProject}
                         state={stateOf(leaf.id)}
                         onFocus={focusWin} onClose={closeWindow} onToggleMax={toggleMax}
                         onMinimize={minimize}
                         onSetState={setCardState} onPicker={openPicker} />
          )
        })}
        {!single && geo.dividers.map((d) => (
          <Divider key={`${d.splitId}:${d.index}`} d={d} stageRef={stageRef}
                   boardRef={boardRef} update={update} />
        ))}
      </div>
    </div>
  )

  return (
    <WorkContext.Provider value={workCtx}>
      <div className={`work${phone ? ' work-phone' : ''}${phoneWin ? ' work-show-window' : ''}`
                      + (maxId ? ' work-max' : '')}>
        {phone && (
          <div className="work-tabs" role="tablist" aria-label="Chat and windows">
            <div className={`work-tab${!phoneWin ? ' on' : ''}`}>
              <button type="button" role="tab" aria-selected={!phoneWin}
                      onClick={() => setTab('chat')}>Chat</button>
            </div>
            {all.map((l) => (
              <div key={l.id} className={`work-tab${phoneWin && tab === l.id ? ' on' : ''}`}>
                <button type="button" role="tab" aria-selected={phoneWin && tab === l.id}
                        onClick={() => { setTab(l.id); focusWin(l.id) }}>
                  {WINDOW_TYPES[l.type]?.title || l.type}</button>
                {phoneWin && tab === l.id && (
                  <button type="button" className="work-tab-x" aria-label="close window"
                          onClick={() => closeWindow(l.id)}>×</button>
                )}
              </div>
            ))}
            {plus}
          </div>
        )}
        <Chat controlRef={chatCtl} onProjectChange={onProjectChange}
              toolbarExtra={phone ? null : plus}
              beside={beside} />
        {picker && (
          <WindowPicker anchor={picker.anchor} project={project} projects={projects}
                        label={picker.mode === 'switch' ? 'Switch this window' : 'Open a window'}
                        full={picker.mode !== 'switch' && !!board && !canAdd(board)}
                        current={picker.mode === 'switch'
                          ? findLeaf(board?.root, picker.target)?.type : undefined}
                        ready={!!board}
                        onPick={onPick} onClose={() => setPicker(null)}
                        onPickProject={(slug) => chatCtl.current?.pickProject(slug)}
                        onProjects={() => { setPicker(null); setSheet(true) }} />
        )}
        {sheet && <ProjectsSheet onClose={() => { setSheet(false); reloadProjects() }} />}
      </div>
    </WorkContext.Provider>
  )
}

// One window: its header and its card, absolutely placed from the layout's
// geometry. Keyed by id and never moved in the React tree, so a split or a
// close elsewhere never remounts it (a running terminal or stream survives).
const WindowFrame = memo(function WindowFrame({
  leaf, slug, other, sub, x, y, w, h, hidden, keep, phone, maximized, focused, project, refreshProject, state,
  onFocus, onClose, onToggleMax, onMinimize, onSetState, onPicker,
}) {
  const id = leaf.id
  const def = WINDOW_TYPES[leaf.type]
  // another project's window: its own project object
  const [otherObj, setOtherObj] = useState(null)
  const [otherGen, setOtherGen] = useState(0)
  useEffect(() => {
    if (!other || other === slug) return undefined
    let live = true
    api(`/api/projects/${encodeURIComponent(other)}`)
      .then((p) => { if (live) setOtherObj(p) }).catch(() => {})
    return () => { live = false }
  }, [other, slug, otherGen])
  const foreign = !!other && other !== slug
  const refreshOther = useCallback(() => { setOtherGen((g) => g + 1); return Promise.resolve() }, [])
  const setState = useCallback((patch) => onSetState(id, patch), [id, onSetState])
  const toggle = useCallback(() => { if (!phone) onToggleMax(id) }, [id, phone, onToggleMax])
  const style = hidden && !keep ? { display: 'none' }
    : { left: pct(x), top: pct(y), width: pct(w), height: pct(h) }
  return (
    <section className={`work-win${focused ? ' focused' : ''}${maximized ? ' max' : ''}`
                        + (hidden && keep ? ' behind' : '')} aria-hidden={hidden || undefined}
             style={style} aria-label={def?.title || leaf.type}
             onPointerDownCapture={() => onFocus(id)} onFocusCapture={() => onFocus(id)}>
      <header className="work-wh" onDoubleClick={(e) => {
        if (!e.target.closest('button')) toggle()
      }}>
        <button type="button" className="work-wh-type" aria-haspopup="dialog"
                title="switch this window to another card"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => onPicker(e, 'switch', id)}>
          <span>{def?.title || leaf.type}{sub && <span className="work-wh-sub"> · {sub}</span>}</span>
          <span className="work-caret" aria-hidden="true">▾</span>
        </button>
        <span className="grow" />
        {!phone && <>
          <button type="button" className="work-wh-btn" title="split right (ctrl+\)"
                  aria-label="split right" onPointerDown={(e) => e.stopPropagation()}
                  onClick={(e) => onPicker(e, 'split', id, 'row')}>⫶</button>
          <button type="button" className="work-wh-btn" title="split down"
                  aria-label="split down" onPointerDown={(e) => e.stopPropagation()}
                  onClick={(e) => onPicker(e, 'split', id, 'col')}>⊟</button>
          <button type="button" className="work-wh-btn" title="minimize"
                  aria-label="minimize" onPointerDown={(e) => e.stopPropagation()}
                  onClick={() => onMinimize(id)}>–</button>
          <button type="button" className="work-wh-btn"
                  aria-label={maximized ? 'restore' : 'maximize'}
                  title={maximized ? 'restore (double-click the header)' : 'maximize (double-click the header)'}
                  onClick={() => onToggleMax(id)}>{maximized ? '▣' : '□'}</button>
        </>}
        <button type="button" className="work-wh-btn" title="close" aria-label="close window"
                onClick={() => onClose(id)}>×</button>
      </header>
      <div className="window-body work-wb">
        <ErrorBoundary resetKey={`${leaf.type}:${foreign ? other : slug}`}>
          <Suspense fallback={<div className="route-pending" aria-busy="true" />}>
            {foreign && !otherObj ? <div className="route-pending" aria-busy="true" /> : (
              <WindowBody type={leaf.type} slug={foreign ? other : slug}
                          project={foreign ? otherObj : project}
                          refreshProject={foreign ? refreshOther : refreshProject}
                          state={state || EMPTY}
                          setState={setState} onToggleExpand={toggle} />
            )}
          </Suspense>
        </ErrorBoundary>
      </div>
    </section>
  )
})

const EMPTY = Object.freeze({})
const EMPTY_LIST = Object.freeze([])

// A draggable boundary between two children of a split. The drag resizes
// from the tree as it was at pointerdown (layout.resize), so it never drifts.
function Divider({ d, stageRef, boardRef, update }) {
  const row = d.dir === 'row'
  const s = d.split
  const style = row
    ? { left: pct(d.pos), top: pct(s.y), height: pct(s.h) }
    : { top: pct(d.pos), left: pct(s.x), width: pct(s.w) }
  const onPointerDown = (e) => {
    if (e.button !== 0) return
    e.preventDefault()
    const stage = stageRef.current?.getBoundingClientRect()
    if (!stage || !boardRef.current) return
    const span = (row ? s.w * stage.width : s.h * stage.height) || 1
    const scale = d.scale || 1          // the visible children's share of the split
    const min = ((row ? MIN_W : MIN_H) / span) * scale
    const next = d.next ?? d.index + 1
    const start = row ? e.clientX : e.clientY
    const startRoot = boardRef.current.root
    const el = e.currentTarget
    el.setPointerCapture?.(e.pointerId)
    document.body.classList.add(row ? 'work-dragging-col' : 'work-dragging-row')
    const move = (ev) => {
      const delta = (((row ? ev.clientX : ev.clientY) - start) / span) * scale
      update((b) => ({ ...b, root: resize(startRoot, d.splitId, d.index, delta, min, next) }))
    }
    const up = () => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      document.body.classList.remove('work-dragging-col', 'work-dragging-row')
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }
  return (
    <div className={`work-div ${row ? 'col' : 'row'}`} style={style}
         role="separator" aria-orientation={row ? 'vertical' : 'horizontal'}
         onPointerDown={onPointerDown}
         onDoubleClick={() => {
           // even the two sides out again
           update((b) => {
             const node = findSplit(b.root, d.splitId)
             if (!node) return b
             const next = d.next ?? d.index + 1
             const half = (node.sizes[d.index] + node.sizes[next]) / 2
             return { ...b, root: resize(b.root, d.splitId, d.index, half - node.sizes[d.index], 0, next) }
           })
         }} />
  )
}

// The + menu (and a header's type ▾): every card type, searchable, arrow keys
// + Enter to pick, Escape or a click outside to close. With no project on the
// chat it offers the projects instead: the windows are a project's.
function WindowPicker({
  anchor, label, current, project, projects, ready, full, onPick, onClose, onPickProject, onProjects,
}) {
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  // a second page of the menu: 'chat' (a conversation), 'proj' (which other
  // project), 'projwin' (that project's windows)
  const [step, setStep] = useState(null)
  const [convos, setConvos] = useState(null)
  const ref = useDismiss(true, onClose)
  const listRef = useRef(null)
  const needle = q.trim().toLowerCase()
  const go = (s) => { setStep(s); setQ(''); setSel(0) }
  useEffect(() => {
    if (step?.kind !== 'chat' || convos) return
    api('/api/conversations').then((r) => setConvos(r.conversations || []))
      .catch(() => setConvos([]))
  }, [step, convos])
  const name = (slug) => projects.find((p) => p.slug === slug)?.name || slug
  const mine = [...projects].sort((a, b) => (b.slug === project) - (a.slug === project))
  const winRows = (slug, skip) => Object.entries(WINDOW_TYPES)
    .filter(([k]) => !skip.includes(k))
    .map(([k, v]) => ({ key: k, text: v.label, run: () => onPick(k, slug ? { project: slug } : {}) }))
  let all
  if (full) all = []
  else if (!project) {
    all = projects.map((p) => ({ key: p.slug, text: p.name || p.slug, run: () => onPickProject(p.slug) }))
  } else if (!step) {
    all = [
      { key: '__chat', text: 'Chat… — any conversation, any project', stay: true,
        run: () => go({ kind: 'chat' }) },
      { key: 'review', text: WINDOW_TYPES.review.label, run: () => onPick('review') },
      ...winRows(null, ['chat', 'review']),
      { key: '__other', text: 'Another project’s windows…', stay: true, run: () => go({ kind: 'proj' }) },
    ]
  } else if (step.kind === 'chat') {
    all = [
      ...mine.map((p) => ({ key: `new:${p.slug}`, text: `New chat in ${p.name || p.slug}`,
        run: () => onPick('chat', { state: { chatProject: p.slug, conversation: 'new' } }) })),
      { key: 'new:', text: 'New chat, no project',
        run: () => onPick('chat', { state: { chatProject: null, conversation: 'new' } }) },
      ...(convos || []).map((c) => ({
        key: `c${c.id}`, text: c.summary || `#${c.id}`,
        sub: c.project_slug ? (c.project_name || c.project_slug) : 'no project',
        run: () => onPick('chat', { state: { chatProject: c.project_slug || null, conversation: c.id } }),
      })),
    ]
  } else if (step.kind === 'proj') {
    all = projects.filter((p) => p.slug !== project).map((p) => ({
      key: `p:${p.slug}`, text: p.name || p.slug, stay: true,
      run: () => go({ kind: 'projwin', slug: p.slug }) }))
  } else {
    all = winRows(step.slug, ['chat'])
  }
  const types = all.filter((r) => !needle || `${r.key} ${r.text} ${r.sub || ''}`.toLowerCase().includes(needle))
  const rows = [...types, step
    ? { key: '__back', text: '← Back', run: () => go(null), stay: true, foot: true }
    : { key: '__projects', text: 'Projects…', run: onProjects, foot: true }]
  const pick = (r) => { if (!r.stay) onClose(); r.run() }

  const [pos, setPos] = useState(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const vw = document.documentElement.clientWidth
    const vh = window.innerHeight
    const w = el.offsetWidth
    const a = anchor || { left: vw / 2 - w / 2, right: vw / 2 + w / 2, bottom: vh / 4 }
    // hang from the anchor's right edge: the + sits at the right of the bar
    const left = Math.max(8, Math.min(a.right - w, vw - w - 8))
    const top = Math.min(a.bottom + 6, vh - 160)
    setPos({ left, top, maxHeight: vh - top - 12 })
  }, [anchor, ref])
  useEffect(() => {
    if (pos) ref.current?.querySelector('input')?.focus()
  }, [pos, ref, step])
  useLayoutEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [sel, q])

  const onKey = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(rows.length - 1, s + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)) }
    else if (e.key === 'Enter' && rows[sel]) { e.preventDefault(); pick(rows[sel]) }
  }
  const row = (r, i) => (
    <button key={r.key} type="button" role="option" aria-selected={i === sel}
            className={`work-pick${i === sel ? ' sel' : ''}${r.key === current ? ' current' : ''}`
                       + (r.foot ? ' foot' : '')}
            onMouseEnter={() => setSel(i)} onClick={() => pick(r)}>
      <span className="grow">{r.text}</span>
      {r.sub && <span className="dim small">{r.sub}</span>}
      {r.key === current && <span className="dim small">current</span>}
    </button>
  )
  return createPortal(
    <div className="work-picker" ref={ref} role="dialog" aria-label={label}
         style={pos || { visibility: 'hidden' }}>
      {!project && (
        <div className="work-picker-note">Pick a project for this chat first —
          its windows open beside the chat.</div>
      )}
      {project && !ready && <div className="work-picker-note dim">loading {project}…</div>}
      {full && <div className="work-picker-note">{MAX_PANELS} windows is the most: close or
        minimize one, or switch one with its header ▾.</div>}

      {step?.kind === 'chat' && !convos && <div className="work-picker-note dim">loading chats…</div>}
      {step?.kind === 'projwin' && <div className="work-picker-note dim">
        {name(step.slug)}’s windows</div>}
      <input className="work-picker-q" value={q}
             placeholder={!project ? 'Pick a project…' : step?.kind === 'chat' ? 'Find a chat…'
               : step?.kind === 'proj' ? 'Pick a project…' : 'Open a window…'}

             aria-label={project ? 'search windows' : 'search projects'} onKeyDown={onKey}
             onChange={(e) => { setQ(e.target.value); setSel(0) }} />
      <div className="work-picker-list" role="listbox" ref={listRef}>
        {types.map(row)}
        {!types.length && !full && <div
 className="dim small work-picker-none">
          {project ? 'no window matches' : 'no project matches'}</div>}
      </div>
      <div className="work-picker-foot">{row(rows.at(-1), rows.length - 1)}</div>
    </div>, document.body)
}

// Projects… — the old Projects page (create, rename, delete, restore,
// import), whole, in a sheet over Work. Opening a project from it lands on
// /projects/:slug, which Work turns into the chat's project.
// Not useDismiss: the page's row menus and its ask() dialogs are portalled to
// <body>, so "a pointerdown outside the sheet" would close it under them. The
// scrim itself and Escape close it (an open dialog's own Escape layer takes
// the key first).
function ProjectsSheet({ onClose }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !e.defaultPrevented) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return createPortal(
    <div className="work-sheet-scrim"
         onPointerDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="work-sheet" role="dialog" aria-label="Projects"
           onClick={(e) => { if (e.target.closest('a[href^="/projects/"]')) onClose() }}>
        <button type="button" className="work-sheet-x icon-btn" aria-label="close"
                onClick={onClose}>×</button>
        <Suspense fallback={<div className="route-pending" aria-busy="true" />}>
          <Projects />
        </Suspense>
      </div>
    </div>, document.body)
}
