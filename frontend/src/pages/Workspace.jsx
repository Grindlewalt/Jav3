import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api.js'
import ChatBox from '../ChatBox.jsx'
import { ReviewQueue } from './Review.jsx'
import { NetworkPanel } from './Network.jsx'
import { notifyError } from '../notify.js'
import PlanPanel from '../PlanPanel.jsx'
import VmStrip from '../VmStrip.jsx'
import Toggle from '../components/Toggle.jsx'
import EmptyState from '../components/EmptyState.jsx'
// the panels live in panels/ so the shell's dock mounts the same components
import JournalPanel from '../panels/JournalPanel.jsx'
import EditorPanel from '../panels/EditorPanel.jsx'
import RendererPanel from '../panels/RendererPanel.jsx'
import OrganizerPanel from '../panels/OrganizerPanel.jsx'
import RunPanel from '../panels/RunPanel.jsx'
import ContextPanel from '../panels/ContextPanel.jsx'
import AgentPanel from '../panels/AgentPanel.jsx'
import ResearchPanel from '../panels/ResearchPanel.jsx'
import GitPanel from '../panels/GitPanel.jsx'
import GrantsPanel from '../panels/GrantsPanel.jsx'
import TerminalPanel from '../panels/TerminalPanel.jsx'
import TaskBoardPanel from '../panels/TaskBoardPanel.jsx'
import TodoPanel from '../panels/TodoPanel.jsx'

// ---- panel registry: add a capability = one component + one entry here ----
const PANEL_TYPES = {
  chat: { label: 'Chat — Jav3 or an agent', w: 440, h: 520 },
  journal: { label: 'Journal — project.md', w: 460, h: 420 },
  editor: { label: 'Editor — text & markdown', w: 520, h: 440 },
  renderer: { label: 'Renderer — html / pdf / images', w: 520, h: 440 },
  organizer: { label: 'File organizer', w: 580, h: 460 },
  run: { label: 'Run — python sandbox', w: 560, h: 470 },
  todos: { label: 'To-dos', w: 360, h: 380 },
  git: { label: 'Git — review, approve, push', w: 560, h: 480 },
  board: { label: 'Task board — goal / plan / runs', w: 400, h: 540 },
  context: { label: 'Context files — load into Jav3', w: 440, h: 460 },
  agent: { label: 'Run an agent', w: 460, h: 520 },
  research: { label: 'Research bots — live', w: 620, h: 560 },
  plan: { label: 'Plan — dump, checklist, agents', w: 560, h: 560 },
  review: { label: 'Security — approvals & alerts', w: 480, h: 540 },
  network: { label: 'Network — egress & host approvals', w: 480, h: 560 },
  secrets: { label: 'Secrets — key grants for this project', w: 460, h: 380 },
  terminal: { label: 'Terminal — shell in the guest VM', w: 560, h: 360 },
}

// Default board: chat + the session spine (board = goal/plan/runs), with git as
// the review/undo surface (writes are live now — no staging panel) and network
// for approving the hosts the agent asks to reach.
//
// It is laid out from the board's own size, not a fixed 1448px block: three
// columns (chat · spine · git over network) when they fit at MIN_W each, else
// chat and spine side by side with git and network under them. The board
// scrolls, so the fallback never clips — it just starts below the fold.
function defaultPanels(boardW, boardH) {
  const P = 16, G = GAP + 4
  const inner = Math.max(2 * MIN_W + G, boardW - 2 * P)
  const h = Math.max(440, Math.min(760, boardH - 2 * P))
  const at = (id, type, x, y, w, hh, z) =>
    ({ id, type, x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(hh), z, state: {} })
  if (inner >= 3 * MIN_W + 2 * G) {
    const c1 = inner * 0.34, c2 = inner * 0.29, c3 = inner - c1 - c2 - 2 * G
    const gh = (h - G) * 0.54
    return [
      at('p1', 'chat', P, P, c1, h, 1),
      at('p2', 'board', P + c1 + G, P, c2, h, 2),
      at('p3', 'git', P + c1 + c2 + 2 * G, P, c3, gh, 3),
      at('p4', 'network', P + c1 + c2 + 2 * G, P + gh + G, c3, h - gh - G, 4),
    ]
  }
  const c = (inner - G) / 2, y2 = P + h + G
  return [
    at('p1', 'chat', P, P, c, h, 1),
    at('p2', 'board', P + c + G, P, c, h, 2),
    at('p3', 'git', P, y2, c, 360, 3),
    at('p4', 'network', P + c + G, y2, c, 360, 4),
  ]
}

// The fixed default every board got before it was fitted. A saved layout that
// is still exactly this was never arranged by anyone — it is the old default
// persisted by the autosave — so it is refitted like an empty one.
const LEGACY_DEFAULT = [
  ['p1', 'chat', 16, 16, 460, 560], ['p2', 'board', 492, 16, 400, 560],
  ['p3', 'git', 908, 16, 540, 300], ['p4', 'network', 908, 332, 540, 244],
]
const isLegacyDefault = (ps) => ps.length === LEGACY_DEFAULT.length &&
  LEGACY_DEFAULT.every(([id, type, x, y, w, h]) => ps.some((p) =>
    p.id === id && p.type === type && p.x === x && p.y === y && p.w === w && p.h === h))

// board grid: drags are smooth, drops snap (matches the dot background)
const GRID = 26
const snap = (v) => Math.round(v / GRID) * GRID
const GAP = 12        // breathing room between tiled panels
const SNAP_T = 16     // px within which an edge becomes magnetic
const MIN_W = 280, MIN_H = 200

// magnetic drop: prefer lining up with other panels' edges, else the grid
function smartPos(me, x, y, others) {
  let bestX = snap(x), bdx = SNAP_T
  let bestY = snap(y), bdy = SNAP_T
  for (const o of others) {
    for (const c of [o.x, o.x + o.w + GAP, o.x + o.w - me.w, o.x - me.w - GAP]) {
      if (Math.abs(x - c) < bdx) { bdx = Math.abs(x - c); bestX = c }
    }
    for (const c of [o.y, o.y + o.h + GAP, o.y + o.h - me.h, o.y - me.h - GAP]) {
      if (Math.abs(y - c) < bdy) { bdy = Math.abs(y - c); bestY = c }
    }
  }
  return { x: Math.max(0, bestX), y: Math.max(0, bestY) }
}

function smartW(me, w, others) {
  let best = snap(w), bd = SNAP_T
  for (const o of others) {
    for (const c of [o.x - GAP - me.x, o.x + o.w - me.x]) {
      if (c >= MIN_W && Math.abs(w - c) < bd) { bd = Math.abs(w - c); best = c }
    }
  }
  return Math.max(MIN_W, best)
}

function smartH(me, h, others) {
  let best = snap(h), bd = SNAP_T
  for (const o of others) {
    for (const c of [o.y - GAP - me.y, o.y + o.h - me.y]) {
      if (c >= MIN_H && Math.abs(h - c) < bd) { bd = Math.abs(h - c); best = c }
    }
  }
  return Math.max(MIN_H, best)
}

const overlapV = (a, b) => a.y < b.y + b.h && a.y + a.h > b.y
const overlapH = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x

// tiling behaviour: growing into a neighbour shrinks it (keeping its far
// edge fixed). Computed from the gesture-start snapshot every frame, so
// dragging back mid-gesture restores neighbours to their original size.
function shrinkAway(p, me0, me) {
  let out = { ...p }
  if (p.x >= me0.x + me0.w - 2 && overlapV(p, me) && me.x + me.w + GAP > p.x) {
    const right = p.x + p.w
    const nx = me.x + me.w + GAP
    out = { ...out, x: nx, w: Math.max(MIN_W, right - nx) }
  }
  if (p.y >= me0.y + me0.h - 2 && overlapH(p, me) && me.y + me.h + GAP > p.y) {
    const bottom = p.y + p.h
    const ny = me.y + me.h + GAP
    out = { ...out, y: ny, h: Math.max(MIN_H, bottom - ny) }
  }
  return out
}

// ---- auto-arrange: shelf-pack the open panels into a tight block ------------
// Panels are taken in reading order and packed into rows. To let rows meet
// flush, each panel may grow up to 2 grid units and shrink up to 1 per axis;
// anything the budget can't close stays as a small hole rather than a
// distorted panel. Several row widths are tried and scored on hole area +
// how far the block's shape drifts from the viewport's.
const GROW = 2 * GRID
const SHRINK = GRID
const ARR_PAD = 16   // block origin — matches the default board inset

const toward = (want, cur, floor) =>
  Math.max(Math.max(floor, cur - SHRINK), Math.min(cur + GROW, want))

function arrangeRows(items, targetW) {
  const rows = []
  let row = [], x = 0
  for (const it of items) {
    const minW = Math.max(MIN_W, it.w0 - SHRINK)
    if (row.length && x + minW > targetW) { rows.push(row); row = []; x = 0 }
    // squeeze (within budget) so the row closes flush on the right edge
    const w = Math.min(it.w0, Math.max(minW, targetW - x))
    row.push({ ...it, w })
    x += w + GAP
  }
  if (row.length) rows.push(row)

  let y = 0, usedW = 0, filled = 0
  const placed = []
  for (const r of rows) {
    // row height: the tallest panel's, or one unit shorter when that leaves
    // strictly less hole under the short neighbours
    const maxH = Math.max(...r.map((o) => o.h0))
    const hole = (rh) => r.reduce((s, o) => s + (rh - toward(rh, o.h0, MIN_H)) * o.w, 0)
    const low = maxH - SHRINK
    const rowH = low >= MIN_H && hole(low) < hole(maxH) ? low : maxH
    // hand leftover row width out a grid step at a time, round-robin
    let leftover = targetW - r.reduce((s, o) => s + o.w, 0) - GAP * (r.length - 1)
    let moved = true
    while (leftover >= GRID && moved) {
      moved = false
      for (const o of r) {
        if (leftover >= GRID && o.w + GRID <= o.w0 + GROW) {
          o.w += GRID; leftover -= GRID; moved = true
        }
      }
    }
    let rx = 0
    for (const o of r) {
      o.x = rx
      o.y = y
      o.h = toward(rowH, o.h0, MIN_H)
      rx += o.w + GAP
      filled += o.w * o.h
      placed.push(o)
    }
    usedW = Math.max(usedW, rx - GAP)
    y += rowH + GAP
  }
  return { placed, w: usedW, h: y - GAP, filled }
}

export default function Workspace() {
  const { slug } = useParams()
  const [project, setProject] = useState(null)
  const [panels, setPanels] = useState(null)
  const [expanded, setExpanded] = useState(null)   // panel id
  const [expandRect, setExpandRect] = useState(null)
  const [menu, setMenu] = useState(null)           // {x, y, bx, by}
  const [hovered, setHovered] = useState(null)     // panel id under the mouse
  const [resizing, setResizing] = useState(false)  // gesture live: no transitions
  const [closingIds, setClosingIds] = useState([]) // panels playing their exit
  const boardRef = useRef(null)
  const zRef = useRef(10)
  const saveTimer = useRef(null)
  const mouseRef = useRef({ x: 200, y: 160 })
  const undoRef = useRef([])                       // ctrl+z stack: closes + pre-tidy layouts
  const gestureRef = useRef(null)                  // layout snapshot during a resize
  const fitRef = useRef(false)                     // lay the default out once the board exists
  const spawnedRef = useRef(new Set())             // panels opened this visit: only these animate in

  const refreshProject = useCallback(
    () => api(`/api/projects/${slug}`).then(setProject), [slug])

  const loadLayout = useCallback(() =>
    api(`/api/projects/${slug}/layout`).then((r) => {
      // drop panel types that no longer exist (e.g. the removed 'staging'
      // panel on an old saved board) so they don't render "unknown panel"
      const p = (r.layout?.panels || []).filter((x) => PANEL_TYPES[x.type])
      if (!p.length || isLegacyDefault(p)) {
        // the default is sized from the board, which has to exist first:
        // render it empty and let the layout effect below fill it pre-paint
        fitRef.current = true
        setPanels([])
        return
      }
      zRef.current = Math.max(10, ...p.map((x) => x.z || 0))
      setPanels(p)
    }), [slug])

  useLayoutEffect(() => {
    const b = boardRef.current
    if (!fitRef.current || !b || !panels) return
    fitRef.current = false
    setPanels(defaultPanels(b.clientWidth, b.clientHeight))
  }, [panels, project])

  useEffect(() => {
    // reset before loading: a stale panels array must never be debounce-saved
    // into the NEW slug's layout (cross-project board bleed)
    setPanels(null)
    // opening a project's board loads it into Jav3's context — this tab is
    // where you live, so what you're looking at is what Jav3 is thinking about.
    // The board renders whether or not that succeeds: a refused load (a 403
    // from the origin gate, say) used to leave `project` null and the page on
    // its spinner forever.
    api(`/api/projects/${slug}/load`, { method: 'POST' })
      .catch(notifyError)
      .then(refreshProject)
    loadLayout()
  }, [slug, refreshProject, loadLayout])

  // Jav3 rearranged the board server-side (workspace_panel tool) — refetch
  useEffect(() => {
    const h = (e) => { if (!e.detail?.slug || e.detail.slug === slug) loadLayout() }
    window.addEventListener('jarvis-layout-changed', h)
    return () => window.removeEventListener('jarvis-layout-changed', h)
  }, [slug, loadLayout])

  // debounced layout persistence
  useEffect(() => {
    if (!panels) return
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      api(`/api/projects/${slug}/layout`, {
        method: 'PUT', body: JSON.stringify({ panels }) })
    }, 800)
    return () => clearTimeout(saveTimer.current)
  }, [panels, slug])

  const patchPanel = (id, patch) =>
    setPanels((ps) => ps.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  const patchState = (id, patch) =>
    setPanels((ps) => ps.map((p) =>
      p.id === id ? { ...p, state: { ...p.state, ...patch } } : p))
  const front = (id) => patchPanel(id, { z: ++zRef.current })

  const dragEnd = (id, x, y) =>
    setPanels((ps) => {
      const me = ps.find((p) => p.id === id)
      const pos = smartPos(me, x, y, ps.filter((p) => p.id !== id))
      return ps.map((p) => (p.id === id ? { ...p, ...pos } : p))
    })

  const resizeStart = (id) => {
    gestureRef.current = { id, snap: panels.map((p) => ({ ...p })) }
    setResizing(true)
  }

  const resizeMove = (id, dx, dy, final) => {
    const snap0 = gestureRef.current?.snap
    if (!snap0) return
    const me0 = snap0.find((p) => p.id === id)
    const others0 = snap0.filter((p) => p.id !== id)
    let w = Math.max(MIN_W, me0.w + dx)
    let h = Math.max(MIN_H, me0.h + dy)
    if (final) {
      w = smartW(me0, w, others0)
      h = smartH(me0, h, others0)
    }
    const me = { ...me0, w, h }
    const resolved = others0.map((p) => shrinkAway(p, me0, me))
    setPanels((ps) => ps.map((cur) => {
      if (cur.id === id) return { ...cur, w, h }
      const r = resolved.find((p) => p.id === cur.id)
      return r ? { ...cur, x: r.x, y: r.y, w: r.w, h: r.h } : cur
    }))
    if (final) {
      gestureRef.current = null
      setResizing(false)
    }
  }

  const close = (id) => {
    if (closingIds.includes(id)) return
    setExpanded((ex) => (ex === id ? null : ex))
    setHovered((h) => (h === id ? null : h))
    setClosingIds((c) => [...c, id])   // play the exit animation first
    setTimeout(() => {
      setClosingIds((c) => c.filter((x) => x !== id))
      setPanels((ps) => {
        const p = ps.find((x) => x.id === id)
        if (p) undoRef.current.push({ kind: 'close', panel: p })
        return ps.filter((x) => x.id !== id)
      })
    }, 170)
  }

  const undo = () => {
    const e = undoRef.current.pop()
    if (!e) return
    if (e.kind === 'close') {
      setPanels((ps) => [...ps, { ...e.panel, z: ++zRef.current }])
    } else {
      // pre-tidy snapshot: restore geometry for panels that still exist;
      // panels opened/closed since keep their own fate (closes are their
      // own undo entries)
      setPanels((ps) => ps.map((p) => {
        const o = e.panels.find((q) => q.id === p.id)
        return o ? { ...p, x: o.x, y: o.y, w: o.w, h: o.h } : p
      }))
    }
  }

  // hover-targeted hotkeys: f expand, q close (ctrl+z restores), n add menu
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') {
        if (menu) setMenu(null)
        else if (expanded) setExpanded(null)
        return
      }
      const t = e.target
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
          t.tagName === 'SELECT' || t.isContentEditable) return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault()
        undo()
        return
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return
      const k = e.key.toLowerCase()
      if (k === 'f' && hovered) { e.preventDefault(); toggleExpand(hovered) }
      else if (k === 'q' && hovered) { e.preventDefault(); close(hovered) }
      else if (k === 'n' && !menu) {
        e.preventDefault()
        openMenuAt(mouseRef.current.x, mouseRef.current.y)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function toggleExpand(id) {
    if (expanded === id) { setExpanded(null); return }
    const b = boardRef.current
    setExpandRect({
      x: b.scrollLeft + 10, y: b.scrollTop + 10,
      w: b.clientWidth - 20, h: b.clientHeight - 20,
    })
    front(id)
    setExpanded(id)
  }

  // spawn placement: use the requested spot if it's genuinely free, else the
  // first grid position in view where the panel fits with breathing room
  function findSpot(w, h, want) {
    const b = boardRef.current
    const x1 = b.scrollLeft + b.clientWidth
    const y1 = b.scrollTop + b.clientHeight
    const free = (x, y) =>
      x >= 0 && y >= 0 && x + w <= x1 - GAP && y + h <= y1 - GAP &&
      !panels.some((r) =>
        x < r.x + r.w + GAP && x + w + GAP > r.x &&
        y < r.y + r.h + GAP && y + h + GAP > r.y)
    if (want && free(want.x, want.y)) return want
    const gx0 = Math.ceil((b.scrollLeft + GAP) / GRID) * GRID
    const gy0 = Math.ceil((b.scrollTop + GAP) / GRID) * GRID
    for (let y = gy0; y + h <= y1; y += GRID)
      for (let x = gx0; x + w <= x1; x += GRID)
        if (free(x, y)) return { x, y }
    return want || { x: gx0 + 2 * GRID, y: gy0 + 2 * GRID }  // board's full: cascade
  }

  function addPanel(type, bx, by) {
    const spec = PANEL_TYPES[type]
    const w = snap(spec.w), h = snap(spec.h)
    const want = bx != null ? { x: snap(Math.max(0, bx)), y: snap(Math.max(0, by)) } : null
    const { x, y } = findSpot(w, h, want)
    const id = `p${Date.now()}`
    spawnedRef.current.add(id)
    setPanels((ps) => [...ps, { id, type, x, y, w, h, z: ++zRef.current, state: {} }])
    setMenu(null)
  }

  // x/y is where the menu wants to sit (AddMenu clamps it into the viewport
  // once it knows its own size); bx/by is the board point a picked panel
  // should spawn at. `alignRight` hangs the menu from its right edge — the
  // header button sits at the right of the bar, so the menu grows leftward.
  function openMenuAt(cx, cy, alignRight = false) {
    const r = boardRef.current.getBoundingClientRect()
    setMenu({
      x: cx, y: cy, alignRight,
      bx: Math.max(0, cx - r.left) + boardRef.current.scrollLeft,
      by: Math.max(0, cy - r.top) + boardRef.current.scrollTop,
    })
  }

  function openMenu(e) {
    e.preventDefault()
    openMenuAt(e.clientX, e.clientY)
  }

  function autoArrange() {
    const b = boardRef.current
    if (!panels?.length || !b) return
    const items = [...panels]
      .sort((a, c) => (a.y - c.y) || (a.x - c.x))
      .map((p) => ({ id: p.id,
                     w0: Math.max(MIN_W, snap(p.w)), h0: Math.max(MIN_H, snap(p.h)) }))
    const floorW = Math.max(...items.map((i) => Math.max(MIN_W, i.w0 - SHRINK)))
    const sumW = items.reduce((s, i) => s + i.w0 + GAP, 0) - GAP
    const maxW = Math.max(floorW, Math.min(b.clientWidth - 2 * ARR_PAD, sumW))
    const aspect = b.clientWidth / Math.max(1, b.clientHeight)
    let best = null
    for (let k = 0; k <= 8; k++) {
      const tw = Math.round(floorW + ((maxW - floorW) * k) / 8)
      const r = arrangeRows(items, tw)
      const box = Math.max(1, r.w * r.h)
      const score = (box - r.filled) / box +
        0.35 * Math.abs(Math.log((r.w / Math.max(1, r.h)) / aspect))
      if (!best || score < best.score) best = { ...r, score }
    }
    undoRef.current.push({ kind: 'layout', panels: panels.map((p) => ({ ...p })) })
    setExpanded(null)
    setPanels((ps) => ps.map((p) => {
      const o = best.placed.find((q) => q.id === p.id)
      return o ? { ...p, x: ARR_PAD + o.x, y: ARR_PAD + o.y, w: o.w, h: o.h } : p
    }))
    b.scrollTo({ top: 0, left: 0, behavior: 'smooth' })
  }

  if (!project || !panels) return <div className="center">…</div>

  return (
    <div className="workspace">
      <header className="ws-head">
        <h1 title={project.name}>{project.name}</h1>
        <Toggle checked={!!project.loaded} label="loaded into Jav3's context"
                onText="in context" offText="not in context"
                title={project.loaded
                  ? 'Jav3 is working in this project — switch off to unload it'
                  : 'load this project into Jav3\'s context'}
                onChange={async (on) => {
                  await api(on ? `/api/projects/${slug}/load` : '/api/projects/unload',
                            { method: 'POST' })
                  refreshProject()
                }} />
        <VmStrip slug={slug} />
        <div className="ws-actions">
          <span className="dim hint">hover + <kbd>f</kbd> expand · <kbd>q</kbd> close
            · <kbd>ctrl+z</kbd> restore · <kbd>n</kbd> / right-click add
            · <kbd>esc</kbd> collapse</span>
          {/* on a phone the words go and the glyphs stay, so both
              actions share one row at touch size */}
          <button className="ghost" onClick={autoArrange}
                  title="auto-arrange the open panels into a tight block (grows ≤2 grid units, shrinks ≤1)">
            ⌗<span className="ws-btn-word"> tidy</span></button>
          <button className="ghost" title="add a panel"
                  onClick={(e) => {
                    const b = e.currentTarget.getBoundingClientRect()
                    openMenuAt(b.right, b.bottom + 8, true)
                  }}>+<span className="ws-btn-word"> panel</span></button>
        </div>
      </header>
      <div className="board" ref={boardRef} onContextMenu={openMenu}
           onPointerMove={(e) => { mouseRef.current = { x: e.clientX, y: e.clientY } }}>
        {panels.map((p) => (
          <Window key={p.id} panel={p}
                  expanded={expanded === p.id} expandRect={expandRect}
                  dimmed={expanded !== null && expanded !== p.id}
                  noAnim={resizing}
                  spawned={spawnedRef.current.has(p.id)}
                  closing={closingIds.includes(p.id)}
                  onPatch={(patch) => patchPanel(p.id, patch)}
                  onDragEnd={(x, y) => dragEnd(p.id, x, y)}
                  onResizeStart={() => resizeStart(p.id)}
                  onResize={(dx, dy, final) => resizeMove(p.id, dx, dy, final)}
                  onFront={() => front(p.id)}
                  onClose={() => close(p.id)}
                  onHover={(over) => setHovered((h) =>
                    over ? p.id : (h === p.id ? null : h))}
                  onToggleExpand={() => toggleExpand(p.id)}>
            <PanelBody type={p.type} slug={slug} project={project}
                       refreshProject={refreshProject}
                       state={p.state || {}}
                       setState={(patch) => patchState(p.id, patch)}
                       onToggleExpand={() => toggleExpand(p.id)} />
          </Window>
        ))}
        {menu && <AddMenu pos={menu} onClose={() => setMenu(null)}
                          onPick={(type) => addPanel(type, menu.bx, menu.by)} />}
      </div>
    </div>
  )
}

function PanelBody(props) {
  switch (props.type) {
    case 'chat': return <ChatBox projectSlug={props.slug} />
    case 'journal': return <JournalPanel {...props} />
    case 'editor': return <EditorPanel {...props} />
    case 'renderer': return <RendererPanel {...props} />
    case 'organizer': return <OrganizerPanel {...props} />
    case 'run': return <RunPanel {...props} />
    case 'todos': return <TodoPanel {...props} />
    case 'git': return <GitPanel {...props} />
    case 'board': return <TaskBoardPanel {...props} />
    case 'context': return <ContextPanel {...props} />
    case 'agent': return <AgentPanel {...props} />
    case 'research': return <ResearchPanel {...props} />
    case 'plan': return <PlanPanel {...props} />
    case 'review': return <ReviewPanel {...props} />
    case 'network': return <NetworkPanel slug={props.slug} />
    case 'secrets': return <GrantsPanel slug={props.slug} />
    case 'terminal': return <TerminalPanel slug={props.slug} />
    default: return <EmptyState pad>unknown panel</EmptyState>
  }
}

// ---- window chrome ----------------------------------------------------------

function Window({ panel, expanded, expandRect, dimmed, noAnim, spawned, closing,
                  onPatch, onDragEnd, onResizeStart, onResize, onFront,
                  onClose, onHover, onToggleExpand, children }) {
  const [interacting, setInteracting] = useState(false)

  function track(e, apply, settle) {
    e.preventDefault()
    onFront()
    setInteracting(true)
    const sx = e.clientX, sy = e.clientY
    let dx = 0, dy = 0
    const move = (ev) => { dx = ev.clientX - sx; dy = ev.clientY - sy; apply(dx, dy) }
    const up = () => {
      setInteracting(false)   // anim class returns, so the snap glides in
      settle(dx, dy)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const startDrag = (e) => {
    if (expanded || e.target.closest('button')) return
    const { x, y } = panel
    track(e,
      (dx, dy) => onPatch({ x: Math.max(0, x + dx), y: Math.max(0, y + dy) }),
      (dx, dy) => onDragEnd(Math.max(0, x + dx), Math.max(0, y + dy)))
  }
  const startResize = (e) => {
    if (expanded) return
    onResizeStart()
    track(e,
      (dx, dy) => onResize(dx, dy, false),
      (dx, dy) => onResize(dx, dy, true))
  }

  const style = expanded && expandRect
    ? { left: expandRect.x, top: expandRect.y, width: expandRect.w,
        height: expandRect.h, zIndex: 999 }
    : { left: panel.x, top: panel.y, width: panel.w, height: panel.h,
        zIndex: panel.z || 1 }

  return (
    <section className={['window', interacting || noAnim ? '' : 'anim', spawned ? 'spawned' : '',
                          expanded ? 'expanded' : '', dimmed ? 'dimmed' : '',
                          closing ? 'closing' : ''].filter(Boolean).join(' ')}
             style={style} onPointerDown={onFront}
             onPointerEnter={() => onHover(true)}
             onPointerLeave={() => onHover(false)}>
      <header className="window-head" onPointerDown={startDrag}
              onDoubleClick={onToggleExpand}>
        <span className="window-title">{PANEL_TYPES[panel.type]?.label || panel.type}</span>
        <button className="win-btn" title={expanded ? 'collapse (esc)' : 'expand'}
                onClick={onToggleExpand}>{expanded ? '⤡' : '⤢'}</button>
        <button className="win-btn" title="close" onClick={onClose}>×</button>
      </header>
      <div className="window-body">{children}</div>
      {!expanded && <div className="resize-handle" onPointerDown={startResize} />}
    </section>
  )
}

// ---- right-click add menu (blender-nodes style, keyboard friendly) ----------

function AddMenu({ pos, onPick, onClose }) {
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const boxRef = useRef(null)
  const items = Object.entries(PANEL_TYPES)
    .filter(([k, v]) => (k + ' ' + v.label).toLowerCase().includes(q.toLowerCase()))

  // Clamp into the viewport once the menu knows its own size (a 16px gutter,
  // like every floating menu): it used to open at click-120px and hang off
  // the left edge of a phone. What does not fit below scrolls in the list.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return
    const G = 16
    const vw = document.documentElement.clientWidth, vh = window.innerHeight
    const w = el.offsetWidth
    const x = pos.alignRight ? pos.x - w : pos.x
    const top = Math.max(G, Math.min(pos.y, vh - G - 240))
    el.style.left = `${Math.max(G, Math.min(x, vw - G - w))}px`
    el.style.top = `${top}px`
    el.style.maxHeight = `${vh - G - top}px`
  }, [pos])

  function onKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, items.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
    else if (e.key === 'Enter' && items[sel]) onPick(items[sel][0])
    else if (e.key === 'Escape') onClose()
  }

  return (
    <>
      <div className="menu-overlay" onMouseDown={onClose} onContextMenu={(e) => { e.preventDefault(); onClose() }} />
      <div className="rc-menu" ref={boxRef} role="dialog" aria-label="add panel">
        <input autoFocus placeholder="add panel — type to search…" value={q}
               aria-label="search panels"
               onChange={(e) => { setQ(e.target.value); setSel(0) }} onKeyDown={onKey} />
        <ul>
          {items.map(([key, v], i) => (
            <li key={key} className={i === sel ? 'sel' : ''}
                onMouseEnter={() => setSel(i)}
                onMouseDown={(e) => { e.preventDefault(); onPick(key) }}>
              {v.label}
              {i === sel && <span className="enter-hint">↵</span>}
            </li>
          ))}
          {items.length === 0 && <EmptyState as="li">no match</EmptyState>}
        </ul>
      </div>
    </>
  )
}

// ---- panels ------------------------------------------------------------------

// The unified Security queue, scoped to this one project: the same commit
// requests, egress host approvals and security alerts (incl. advisory write
// flags) the global /security page shows, filtered to this slug.
function ReviewPanel({ slug }) {
  return (
    <div className="pane-col">
      <div className="review-scrollwrap"><ReviewQueue slug={slug} /></div>
    </div>
  )
}

