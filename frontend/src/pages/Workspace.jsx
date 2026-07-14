import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, chatStream } from '../api.js'
import ChatBox from '../ChatBox.jsx'
import Md from '../Md.jsx'

// ---- panel registry: add a capability = one component + one entry here ----
const PANEL_TYPES = {
  chat: { label: 'Jarvis chat', w: 440, h: 520 },
  journal: { label: 'Journal — project.md', w: 460, h: 420 },
  editor: { label: 'Editor — text & markdown', w: 520, h: 440 },
  renderer: { label: 'Renderer — html / pdf / images', w: 520, h: 440 },
  organizer: { label: 'File organizer', w: 580, h: 460 },
  run: { label: 'Run — python sandbox', w: 560, h: 470 },
  todos: { label: 'To-dos', w: 360, h: 380 },
  staging: { label: 'Staged changes — approve / reject', w: 620, h: 480 },
  context: { label: 'Context files — load into Jarvis', w: 440, h: 460 },
  agent: { label: 'Run an agent', w: 460, h: 520 },
  research: { label: 'Research bots — live', w: 620, h: 560 },
}

const DEFAULT_PANELS = [
  { id: 'p1', type: 'chat', x: 16, y: 16, w: 440, h: 520, z: 1, state: {} },
  { id: 'p2', type: 'journal', x: 472, y: 16, w: 440, h: 300, z: 2, state: {} },
  { id: 'p3', type: 'todos', x: 928, y: 16, w: 340, h: 300, z: 3, state: {} },
  { id: 'p4', type: 'staging', x: 472, y: 332, w: 620, h: 204, z: 4, state: {} },
]

const TEXT_EXT = /\.(md|txt|py|js|jsx|ts|json|html|css|csv|toml|yaml|yml|sh|tex)$/i
const IMG_EXT = /\.(png|jpg|jpeg|gif|svg|webp)$/i
const MEDIA_EXT = /\.(html?|pdf|png|jpg|jpeg|gif|svg|webp)$/i

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

const rawUrl = (slug, p) =>
  `/api/projects/${slug}/raw/${p.split('/').map(encodeURIComponent).join('/')}`

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
  const undoRef = useRef([])                       // closed panels, for ctrl+z
  const gestureRef = useRef(null)                  // layout snapshot during a resize

  const refreshProject = useCallback(
    () => api(`/api/projects/${slug}`).then(setProject), [slug])

  useEffect(() => {
    // opening a project's board loads it into Jarvis's context — this tab is
    // where you live, so what you're looking at is what Jarvis is thinking about
    api(`/api/projects/${slug}/load`, { method: 'POST' }).then(refreshProject)
    api(`/api/projects/${slug}/layout`).then((r) => {
      const p = r.layout?.panels?.length ? r.layout.panels : DEFAULT_PANELS
      zRef.current = Math.max(10, ...p.map((x) => x.z || 0))
      setPanels(p)
    })
  }, [slug, refreshProject])

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
        if (p) undoRef.current.push(p)
        return ps.filter((x) => x.id !== id)
      })
    }, 170)
  }

  const undoClose = () => {
    const p = undoRef.current.pop()
    if (p) setPanels((ps) => [...ps, { ...p, z: ++zRef.current }])
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
        undoClose()
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
    setPanels((ps) => [...ps, {
      id: `p${Date.now()}`, type, x, y, w, h, z: ++zRef.current, state: {},
    }])
    setMenu(null)
  }

  function openMenuAt(cx, cy) {
    const r = boardRef.current.getBoundingClientRect()
    setMenu({
      x: Math.min(cx, window.innerWidth - 280),
      y: Math.min(cy, window.innerHeight - 340),
      bx: cx - r.left + boardRef.current.scrollLeft,
      by: cy - r.top + boardRef.current.scrollTop,
    })
  }

  function openMenu(e) {
    e.preventDefault()
    openMenuAt(e.clientX, e.clientY)
  }

  if (!project || !panels) return <div className="center">…</div>

  return (
    <div className="workspace">
      <header className="ws-head">
        <h2>{project.name}</h2>
        {project.loaded
          ? <button className="ghost" onClick={async () => {
              await api('/api/projects/unload', { method: 'POST' }); refreshProject() }}>
              in context ✓ (unload)</button>
          : <button className="ghost" onClick={async () => {
              await api(`/api/projects/${slug}/load`, { method: 'POST' }); refreshProject() }}>
              load into context</button>}
        <span className="dim hint">hover + <kbd>f</kbd> expand · <kbd>q</kbd> close ·
          <kbd> ctrl+z</kbd> restore · <kbd>n</kbd> / right-click add ·
          <kbd> esc</kbd> collapse</span>
        <button className="ghost" onClick={(e) => openMenuAt(e.clientX - 120, e.clientY + 14)}>
          + panel</button>
      </header>
      <div className="board" ref={boardRef} onContextMenu={openMenu}
           onPointerMove={(e) => { mouseRef.current = { x: e.clientX, y: e.clientY } }}>
        {panels.map((p) => (
          <Window key={p.id} panel={p}
                  expanded={expanded === p.id} expandRect={expandRect}
                  dimmed={expanded !== null && expanded !== p.id}
                  noAnim={resizing}
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
    case 'staging': return <StagingPanel {...props} />
    case 'context': return <ContextPanel {...props} />
    case 'agent': return <AgentPanel {...props} />
    case 'research': return <ResearchPanel {...props} />
    default: return <div className="dim">unknown panel</div>
  }
}

// ---- window chrome ----------------------------------------------------------

function Window({ panel, expanded, expandRect, dimmed, noAnim, closing,
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
    <section className={`window ${interacting || noAnim ? '' : 'anim'} ${expanded ? 'expanded' : ''} ${dimmed ? 'dimmed' : ''} ${closing ? 'closing' : ''}`}
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
  const items = Object.entries(PANEL_TYPES)
    .filter(([k, v]) => (k + ' ' + v.label).toLowerCase().includes(q.toLowerCase()))

  function onKey(e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, items.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
    else if (e.key === 'Enter' && items[sel]) onPick(items[sel][0])
    else if (e.key === 'Escape') onClose()
  }

  return (
    <>
      <div className="menu-overlay" onMouseDown={onClose} onContextMenu={(e) => { e.preventDefault(); onClose() }} />
      <div className="rc-menu" style={{ left: pos.x, top: pos.y }}>
        <input autoFocus placeholder="add panel — type to search…" value={q}
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
          {items.length === 0 && <li className="dim">no match</li>}
        </ul>
      </div>
    </>
  )
}

// ---- panels ------------------------------------------------------------------

function JournalPanel({ slug, project, refreshProject }) {
  const [md, setMd] = useState(project.project_md)
  const [dirty, setDirty] = useState(false)
  async function save() {
    await api(`/api/projects/${slug}/md`, {
      method: 'PUT', body: JSON.stringify({ content: md }) })
    setDirty(false)
    refreshProject()
  }
  return (
    <div className="pane-col">
      <textarea className="md-editor grow" spellCheck={false} value={md}
                onChange={(e) => { setMd(e.target.value); setDirty(true) }} />
      <div className="row">
        <span className="dim grow">the journal Jarvis loads with this project</span>
        <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
      </div>
    </div>
  )
}

function EditorPanel({ slug, state, setState }) {
  const [files, setFiles] = useState([])
  const [content, setContent] = useState('')
  const [binary, setBinary] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [preview, setPreview] = useState(false)
  const path = state.path || ''
  const previewable = /\.(md|txt)$/i.test(path)

  const refresh = useCallback(() =>
    api(`/api/projects/${slug}/files`).then((r) =>
      setFiles(r.files.map((f) => f.path).filter((p) => TEXT_EXT.test(p)))), [slug])
  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    if (!path) { setContent(''); return }
    api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`)
      .then((r) => { setBinary(r.binary); setContent(r.binary ? '' : r.content); setDirty(false) })
      .catch(() => { setContent(''); setState({ path: '' }) })
  }, [slug, path]) // eslint-disable-line

  async function save() {
    await api(`/api/projects/${slug}/file`, {
      method: 'PUT', body: JSON.stringify({ path, content }) })
    setDirty(false)
  }
  async function newFile() {
    const p = window.prompt('new file path (e.g. notes/plan.md, code/sim.py)')
    if (!p) return
    await api(`/api/projects/${slug}/file`, {
      method: 'PUT', body: JSON.stringify({ path: p, content: '' }) })
    await refresh()
    setState({ path: p })
  }

  return (
    <div className="pane-col">
      <div className="row">
        <select className="grow" value={path} onChange={(e) => setState({ path: e.target.value })}>
          <option value="">— pick a file —</option>
          {files.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="ghost" onClick={refresh} title="refresh">↻</button>
        <button className="ghost" onClick={newFile}>+ new</button>
        {previewable && (
          <button className="ghost" title={preview ? 'edit' : 'rendered preview'}
                  onClick={() => setPreview((v) => !v)}>{preview ? '✎' : '👁'}</button>
        )}
        <button onClick={save} disabled={!dirty || !path}>{dirty ? 'Save' : 'Saved'}</button>
      </div>
      {binary
        ? <div className="dim center-pad">binary file</div>
        : preview && previewable
          ? (/\.md$/i.test(path)
              ? <div className="md-preview grow"><Md text={content} /></div>
              : <pre className="md-preview grow">{content}</pre>)
          : <textarea className="md-editor grow" spellCheck={false} value={content}
                    disabled={!path}
                    placeholder="pick or create a file…"
                    onChange={(e) => { setContent(e.target.value); setDirty(true) }} />}
    </div>
  )
}

function RendererPanel({ slug, state, setState, onToggleExpand }) {
  const [files, setFiles] = useState([])
  const [html, setHtml] = useState('')
  const path = state.path || ''

  const refresh = useCallback(() =>
    api(`/api/projects/${slug}/files`).then((r) =>
      setFiles(r.files.map((f) => f.path).filter((p) => MEDIA_EXT.test(p)))), [slug])
  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    if (path && /\.html?$/i.test(path)) {
      api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`)
        .then((r) => setHtml(r.content || ''))
        .catch(() => setHtml(''))
    }
  }, [slug, path])

  const url = path && rawUrl(slug, path)
  return (
    <div className="pane-col">
      <div className="row">
        <select className="grow" value={path} onChange={(e) => setState({ path: e.target.value })}>
          <option value="">— pick html / pdf / image —</option>
          {files.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="ghost" onClick={refresh} title="refresh">↻</button>
        {url && <a className="ghost-link" href={url} target="_blank" rel="noreferrer">raw</a>}
      </div>
      <div className="render-area" onDoubleClick={onToggleExpand}>
        {!path ? (
          <div className="dim center-pad">nothing selected — plots, PDFs and pages the
            run sandbox produces show up in this list</div>
        ) : /\.html?$/i.test(path) ? (
          <iframe className="preview-frame" sandbox="allow-scripts" title="preview" srcDoc={html} />
        ) : path.endsWith('.pdf') ? (
          <embed className="preview-frame" src={url} type="application/pdf" />
        ) : (
          <div className="preview-scroll"><img src={url} alt={path} /></div>
        )}
      </div>
    </div>
  )
}

function OrganizerPanel({ slug }) {
  const [dirs, setDirs] = useState([])
  const [files, setFiles] = useState([])
  const [over, setOver] = useState(null)
  const uploadRef = useRef(null)
  const uploadDest = useRef('')

  const refresh = useCallback(async () => {
    const [d, f] = await Promise.all([
      api(`/api/projects/${slug}/dirs`), api(`/api/projects/${slug}/files`)])
    setDirs(d.dirs)
    setFiles(f.files.map((x) => x.path))
  }, [slug])
  useEffect(() => { refresh() }, [refresh])

  const inDir = (dir) => files.filter((p) =>
    (dir === '' ? !p.includes('/') : p.startsWith(dir + '/') &&
      !p.slice(dir.length + 1).includes('/')))

  async function drop(e, dir) {
    e.preventDefault()
    setOver(null)
    const src = e.dataTransfer.getData('text/plain')
    if (!src) return
    const dest = (dir ? dir + '/' : '') + src.split('/').pop()
    if (dest === src) return
    try {
      await api(`/api/projects/${slug}/move`, {
        method: 'POST', body: JSON.stringify({ src, dest }) })
    } catch (err) { window.alert(err.detail || err) }
    refresh()
  }

  async function newDir() {
    const path = window.prompt('new directory (e.g. images, docs/refs)')
    if (!path) return
    const mark = window.prompt('mark for Jarvis — what belongs here? (optional)') || ''
    await api(`/api/projects/${slug}/mkdir`, {
      method: 'POST', body: JSON.stringify({ path, mark }) })
    refresh()
  }

  async function editMark(dir) {
    const mark = window.prompt(
      `mark for ${dir.path || 'project root'} — tell Jarvis what goes here`, dir.mark)
    if (mark === null) return
    await api(`/api/projects/${slug}/dirs/mark`, {
      method: 'PUT', body: JSON.stringify({ path: dir.path, mark }) })
    refresh()
  }

  async function rmDir(dir) {
    try {
      await api(`/api/projects/${slug}/dirs?path=${encodeURIComponent(dir)}`,
                { method: 'DELETE' })
    } catch (err) { window.alert(err.detail || err) }
    refresh()
  }

  async function del(path) {
    if (!window.confirm(`delete ${path}?`)) return
    await api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`,
              { method: 'DELETE' })
    refresh()
  }

  async function onUpload(e) {
    const file = e.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    await fetch(`/api/projects/${slug}/upload?dest=${encodeURIComponent(uploadDest.current)}`,
                { method: 'POST', body: form })
    e.target.value = ''
    refresh()
  }

  return (
    <div className="pane-col">
      <div className="row">
        <span className="dim grow">drag files between directories · marks tell Jarvis
          what belongs where</span>
        <button className="ghost" onClick={newDir}>+ dir</button>
      </div>
      <input ref={uploadRef} type="file" hidden onChange={onUpload} />
      <div className="org-scroll">
        {dirs.map((d) => (
          <div key={d.path}
               className={`dir-card ${over === d.path ? 'drop-over' : ''}`}
               onDragOver={(e) => { e.preventDefault(); setOver(d.path) }}
               onDragLeave={() => setOver(null)}
               onDrop={(e) => drop(e, d.path)}>
            <div className="dir-head">
              <span className="dir-name">📁 {d.path || 'project root'}</span>
              <span className="dir-mark" onClick={() => editMark(d)}
                    title="click to edit the mark Jarvis reads">
                {d.mark || 'no mark — click to add'}
              </span>
              <button className="win-btn" title="upload here"
                      onClick={() => { uploadDest.current = d.path; uploadRef.current.click() }}>⤒</button>
              {d.path && inDir(d.path).length === 0 &&
                <button className="win-btn" title="remove empty dir"
                        onClick={() => rmDir(d.path)}>×</button>}
            </div>
            {inDir(d.path).map((p) => (
              <div key={p} className="file-row" draggable
                   onDragStart={(e) => e.dataTransfer.setData('text/plain', p)}>
                <span className="grow">{p.split('/').pop()}</span>
                <button className="win-btn" onClick={() => del(p)}>×</button>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

function RunPanel({ slug, state, setState }) {
  const [pyFiles, setPyFiles] = useState([])
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  const code = state.code ??
    '# scratch pad — numpy / matplotlib / sympy / pandas / reportlab available\nprint("hello")\n'
  const runFile = state.runFile || ''

  useEffect(() => {
    api(`/api/projects/${slug}/files`).then((r) =>
      setPyFiles(r.files.map((f) => f.path).filter((p) => p.endsWith('.py'))))
  }, [slug, result])

  async function run(body) {
    setBusy(true)
    setResult(null)
    try {
      setResult(await api(`/api/projects/${slug}/run`, {
        method: 'POST', body: JSON.stringify(body) }))
    } catch (err) {
      setResult({ exit_code: -1, stdout: '', stderr: err.detail || String(err), artifacts: [] })
    }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      <textarea className="md-editor code grow" spellCheck={false} value={code}
                onChange={(e) => setState({ code: e.target.value })} />
      <div className="row">
        <button onClick={() => run({ code })} disabled={busy}>
          {busy ? 'running…' : '▶ scratch'}</button>
        <select className="grow" value={runFile}
                onChange={(e) => setState({ runFile: e.target.value })}>
          <option value="">— or a .py file —</option>
          {pyFiles.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="ghost" disabled={!runFile || busy}
                onClick={() => run({ path: runFile })}>▶ file</button>
      </div>
      {result && (
        <div className="run-result">
          <div className="dim">exit {result.exit_code} · {result.duration}s
            {result.timed_out && <span className="warn"> · timed out</span>}</div>
          {result.stdout && <pre className="console">{result.stdout}</pre>}
          {result.stderr && <pre className="console err">{result.stderr}</pre>}
          {result.artifacts?.length > 0 && result.artifacts.map((a) => (
            <div key={a} className="artifact">
              <a href={rawUrl(slug, a)} target="_blank" rel="noreferrer">{a}</a>
              {IMG_EXT.test(a) && <img src={`${rawUrl(slug, a)}?t=${Date.now()}`} alt={a} />}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Pick which project files are loaded into Jarvis's context. Nothing is
// loaded by default — tick a file to include its full contents; the token
// count and running total keep you honest about how big the context gets.
function ContextPanel({ slug }) {
  const [files, setFiles] = useState([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    api(`/api/projects/${slug}/context`).then((r) => {
      setFiles(r.files)
      setTotal(r.selected_tokens)
    })
  useEffect(() => {
    refresh()
    const h = () => refresh()
    window.addEventListener('jarvis-files-changed', h)
    return () => window.removeEventListener('jarvis-files-changed', h)
  }, [slug]) // eslint-disable-line

  async function toggle(path) {
    setBusy(true)
    const next = files.some((f) => f.path === path && f.selected)
      ? files.filter((f) => f.selected && f.path !== path).map((f) => f.path)
      : [...files.filter((f) => f.selected).map((f) => f.path), path]
    try {
      await api(`/api/projects/${slug}/context`, {
        method: 'PUT', body: JSON.stringify({ files: next }) })
      await refresh()
    } catch (err) { window.alert(err.detail || String(err)) }
    setBusy(false)
  }

  const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`)

  return (
    <div className="pane-col">
      <div className="row">
        <span className="grow dim">nothing loads by default — tick to include</span>
        <span className="ctx-total">≈{fmt(total)} tokens loaded</span>
      </div>
      <ul className="ctx-list">
        {files.length === 0 && <li className="dim">no files in this project yet</li>}
        {files.map((f) => (
          <li key={f.path} className={f.selected ? 'on' : ''}>
            <label>
              <input type="checkbox" checked={f.selected} disabled={f.binary || busy}
                     onChange={() => toggle(f.path)} />
              <span className="grow ellipsis">{f.path}</span>
            </label>
            <span className="ctx-tokens">{f.binary ? 'binary' : `≈${fmt(f.tokens)}`}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Run any defined agent right here in the project. It works in this project's
// context and its file edits land in the same staging/approval queue.
function AgentPanel({ slug, state, setState }) {
  const [agents, setAgents] = useState([])
  const [task, setTask] = useState('')
  const [log, setLog] = useState([])
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef(null)
  const which = state.agent || ''

  useEffect(() => { api('/api/agents').then((r) => setAgents(r.agents)) }, [])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [log])

  async function run(confirmPeak = false) {
    if (!which || !task.trim() || busy) return
    setBusy(true)
    setLog((l) => [...l, { role: 'task', text: task }, { role: 'out', text: '' }])
    try {
      await chatStream(
        { task, confirm_peak: confirmPeak }, (ev) => {
          if (ev.type === 'tool')
            setLog((l) => upLast(l, (last) => ({ ...last, text: last.text + `\n⚙ ${ev.name}\n` })))
          if (ev.type === 'token')
            setLog((l) => upLast(l, (last) => ({ ...last, text: last.text + ev.text })))
          if (ev.type === 'final')
            setLog((l) => upLast(l, () => ({ role: 'out', text: ev.content })))
          if (ev.type === 'error')
            setLog((l) => upLast(l, () => ({ role: 'err', text: ev.message })))
        }, `/api/agents/${which}/run`)
      setTask('')
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) {
      setLog((l) => l.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        if (window.confirm('Peak pricing right now — 2x cost. Run the agent anyway?')) {
          setBusy(false); await run(true); return
        }
      } else setLog((l) => [...l, { role: 'err', text: err.detail || String(err) }])
    }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      <div className="row">
        <select className="grow" value={which}
                onChange={(e) => setState({ agent: e.target.value })}>
          <option value="">— pick an agent —</option>
          {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
        </select>
      </div>
      <div className="messages compact">
        {log.length === 0 && <div className="dim center-pad">
          {agents.length ? 'pick an agent and give it a task' : 'no agents yet — create one in the Agents tab'}</div>}
        {log.map((m, i) => (
          <div key={i} className={`msg ${m.role === 'task' ? 'user' : m.role === 'err' ? 'error' : 'assistant'}`}>
            {m.role === 'out'
              ? <div className="bubble"><Md text={m.text || (busy ? '…' : '')} /></div>
              : <pre>{m.text || (busy ? '…' : '')}</pre>}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
      <form className="row" onSubmit={(e) => { e.preventDefault(); run() }}>
        <textarea className="grow" rows={2} value={task} placeholder="task for the agent…"
                  onChange={(e) => setTask(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); run() } }} />
        <button type="submit" disabled={busy || !which}>{busy ? '…' : 'Run'}</button>
      </form>
    </div>
  )
}

// Watch the funnel work: a topic decomposes into a tree of bots (head ->
// leaders -> subagents) that light up live. Each node shows its status, current
// tool activity, and an expandable rollup (cascading fidelity: collapsed by
// default). Built purely from the streamed node_spawned parent/child links.
const STATUS_TAG = {
  planning: 'planning', delegating: 'planning', running: 'running',
  summarizing: 'running', done: 'done', error: 'error',
}
function ResearchPanel({ slug, state, setState }) {
  const [nodes, setNodes] = useState({})   // id -> {id,parent,kind,title,depth,status,tool,rollup}
  const [order, setOrder] = useState([])   // node ids in spawn order
  const [open, setOpen] = useState({})      // id -> rollup expanded
  const [busy, setBusy] = useState(false)
  const [doc, setDoc] = useState(null)
  const topic = state.topic || ''
  const angles = state.angles || 4

  const upNode = (id, patch) =>
    setNodes((n) => ({ ...n, [id]: { ...(n[id] || {}), ...patch } }))

  async function run(confirmPeak = false) {
    if (!topic.trim() || busy) return
    setBusy(true); setNodes({}); setOrder([]); setOpen({}); setDoc(null)
    try {
      await chatStream({ topic, angles: Number(angles) || 4, confirm_peak: confirmPeak }, (ev) => {
        if (ev.type === 'node_spawned') {
          upNode(ev.node_id, { id: ev.node_id, parent: ev.parent_id, kind: ev.kind,
                               title: ev.title, depth: ev.depth, status: 'planning' })
          setOrder((o) => o.includes(ev.node_id) ? o : [...o, ev.node_id])
        }
        if (ev.type === 'node_status') upNode(ev.node_id, { status: ev.status })
        if (ev.type === 'tool') upNode(ev.node_id, { tool: ev.name })
        if (ev.type === 'node_done') upNode(ev.node_id, { status: 'done', rollup: ev.rollup, tool: null })
        if (ev.type === 'error') upNode(ev.node_id, { status: 'error', tool: ev.message })
        if (ev.type === 'job_final') setDoc({ path: ev.doc_path, usage: ev.usage })
      }, '/api/runs/research')
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) {
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        if (window.confirm('Peak pricing right now — 2x cost. Run the research anyway?')) {
          setBusy(false); await run(true); return
        }
      } else window.alert(err.detail || String(err))
    }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      <form className="row" onSubmit={(e) => { e.preventDefault(); run() }}>
        <input className="grow" placeholder="research topic…" value={topic}
               onChange={(e) => setState({ topic: e.target.value })} />
        <input type="number" min="2" max="6" value={angles} style={{ width: '3.5em' }}
               title="angles" onChange={(e) => setState({ angles: e.target.value })} />
        <button type="submit" disabled={busy || !topic.trim()}>{busy ? '…' : 'Research'}</button>
      </form>
      <div className="run-tree">
        {order.length === 0 && <div className="dim center-pad">
          give a topic and watch the bots divide it up</div>}
        {order.map((id) => {
          const n = nodes[id]; if (!n) return null
          return (
            <div key={id} className="run-node" style={{ marginLeft: (n.depth || 0) * 16 }}>
              <div className="run-row" onClick={() => n.rollup && setOpen((o) => ({ ...o, [id]: !o[id] }))}>
                <span className={`tag ${STATUS_TAG[n.status] || 'planning'}`}>{n.kind}</span>
                <span className="grow ellipsis">{n.title}</span>
                {n.tool && <span className="run-activity">⚙ {n.tool}</span>}
                <span className={`run-dot ${STATUS_TAG[n.status] || 'planning'}`} />
                {n.rollup && <span className="dim">{open[id] ? '▾' : '▸'}</span>}
              </div>
              {open[id] && n.rollup && <div className="run-rollup"><Md text={n.rollup} /></div>}
            </div>
          )
        })}
      </div>
      {doc && <div className="dim small">document staged at <code>{doc.path}</code> — approve it in Staged changes
        {doc.usage && <> · {doc.usage}</>}</div>}
    </div>
  )
}

function upLast(list, fn) {
  const copy = [...list]
  copy[copy.length - 1] = fn(copy[copy.length - 1])
  return copy
}

// Jarvis's pending edits: everything it writes lands here first, inert, and
// only touches the real files when approved. Diffs are shown as plain text.
function StagingPanel({ slug }) {
  const [staged, setStaged] = useState([])
  const [sel, setSel] = useState(null)
  const [diff, setDiff] = useState(null)
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    api(`/api/projects/${slug}/staging`).then((r) => {
      setStaged(r.staged)
      if (sel && !r.staged.some((e) => e.path === sel)) { setSel(null); setDiff(null) }
    })
  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 8000)
    const h = () => refresh()
    window.addEventListener('jarvis-files-changed', h)
    return () => { clearInterval(t); window.removeEventListener('jarvis-files-changed', h) }
  }, [slug]) // eslint-disable-line

  useEffect(() => {
    if (!sel) return
    api(`/api/projects/${slug}/staging/diff?path=${encodeURIComponent(sel)}`)
      .then(setDiff).catch(() => setDiff(null))
  }, [sel, slug])

  async function act(verb, paths) {
    setBusy(true)
    try {
      await api(`/api/projects/${slug}/staging/${verb}`, {
        method: 'POST', body: JSON.stringify({ paths }) })
      await refresh()
      window.dispatchEvent(new Event('jarvis-files-changed'))
    } catch (err) { window.alert(err.detail || String(err)) }
    setBusy(false)
  }

  if (staged.length === 0)
    return <div className="dim center-pad">no staged changes — Jarvis's edits appear here for approval</div>

  return (
    <div className="pane-col">
      <div className="row">
        <span className="grow dim">{staged.length} pending file{staged.length !== 1 && 's'}</span>
        <button disabled={busy} onClick={() => act('approve', null)}>✓ approve all</button>
        <button className="ghost danger" disabled={busy}
                onClick={() => window.confirm('discard ALL staged changes?') && act('reject', null)}>
          ✕ reject all</button>
      </div>
      <ul className="staged-list">
        {staged.map((e) => (
          <li key={e.path} className={sel === e.path ? 'active' : ''}
              onClick={() => setSel(e.path)}>
            <span className={`tag ${e.status}`}>{e.status}</span>
            <span className="grow ellipsis">{e.path}</span>
            <button className="win-btn ok" title="approve" disabled={busy}
                    onClick={(ev) => { ev.stopPropagation(); act('approve', [e.path]) }}>✓</button>
            <button className="win-btn" title="reject" disabled={busy}
                    onClick={(ev) => { ev.stopPropagation(); act('reject', [e.path]) }}>✕</button>
          </li>
        ))}
      </ul>
      {sel && diff && (
        <div className="diff-view">
          <div className="diff-col">
            <div className="dim small">current</div>
            <pre>{diff.old ?? '(new file)'}</pre>
          </div>
          <div className="diff-col">
            <div className="dim small">staged</div>
            <pre>{diff.new}</pre>
          </div>
        </div>
      )}
    </div>
  )
}

function TodoPanel({ slug }) {
  const [todos, setTodos] = useState([])
  const [text, setText] = useState('')

  useEffect(() => {
    api(`/api/projects/${slug}/todos`).then((r) => setTodos(r.todos))
  }, [slug])

  async function act(body) {
    const r = await api(`/api/projects/${slug}/todos`, {
      method: 'POST', body: JSON.stringify(body) })
    setTodos(r.todos)
  }

  return (
    <div className="pane-col">
      <form className="row" onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) { act({ action: 'add', text }); setText('') }
      }}>
        <input className="grow" placeholder="add a to-do…" value={text}
               onChange={(e) => setText(e.target.value)} />
        <button type="submit">Add</button>
      </form>
      <ul className="todo-list">
        {todos.map((t, i) => (
          <li key={i} className={t.done ? 'done' : ''}>
            <label>
              <input type="checkbox" checked={t.done}
                     onChange={() => act({ action: 'toggle', index: i })} />
              <span>{t.text}</span>
            </label>
            <button className="win-btn" onClick={() => act({ action: 'delete', index: i })}>×</button>
          </li>
        ))}
        {todos.length === 0 && <li className="dim">nothing yet</li>}
      </ul>
    </div>
  )
}
