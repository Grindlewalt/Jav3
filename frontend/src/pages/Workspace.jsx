import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api.js'

// ---- panel registry: add a capability = one component + one entry here ----
const PANEL_TYPES = {
  journal: { label: 'Journal — project.md', w: 460, h: 420 },
  editor: { label: 'Editor — text & markdown', w: 520, h: 440 },
  renderer: { label: 'Renderer — html / pdf / images', w: 520, h: 440 },
  organizer: { label: 'File organizer', w: 580, h: 460 },
  run: { label: 'Run — python sandbox', w: 560, h: 470 },
  todos: { label: 'To-dos', w: 360, h: 380 },
}

const DEFAULT_PANELS = [
  { id: 'p1', type: 'journal', x: 16, y: 16, w: 460, h: 460, z: 1, state: {} },
  { id: 'p2', type: 'organizer', x: 492, y: 16, w: 560, h: 460, z: 2, state: {} },
  { id: 'p3', type: 'todos', x: 1068, y: 16, w: 340, h: 460, z: 3, state: {} },
]

const TEXT_EXT = /\.(md|txt|py|js|jsx|ts|json|html|css|csv|toml|yaml|yml|sh|tex)$/i
const IMG_EXT = /\.(png|jpg|jpeg|gif|svg|webp)$/i
const MEDIA_EXT = /\.(html?|pdf|png|jpg|jpeg|gif|svg|webp)$/i

const rawUrl = (slug, p) =>
  `/api/projects/${slug}/raw/${p.split('/').map(encodeURIComponent).join('/')}`

export default function Workspace() {
  const { slug } = useParams()
  const [project, setProject] = useState(null)
  const [panels, setPanels] = useState(null)
  const [expanded, setExpanded] = useState(null)   // panel id
  const [expandRect, setExpandRect] = useState(null)
  const [menu, setMenu] = useState(null)           // {x, y, bx, by}
  const boardRef = useRef(null)
  const zRef = useRef(10)
  const saveTimer = useRef(null)

  const refreshProject = useCallback(
    () => api(`/api/projects/${slug}`).then(setProject), [slug])

  useEffect(() => {
    refreshProject()
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

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      if (menu) setMenu(null)
      else if (expanded) setExpanded(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menu, expanded])

  const patchPanel = (id, patch) =>
    setPanels((ps) => ps.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  const patchState = (id, patch) =>
    setPanels((ps) => ps.map((p) =>
      p.id === id ? { ...p, state: { ...p.state, ...patch } } : p))
  const front = (id) => patchPanel(id, { z: ++zRef.current })
  const close = (id) => {
    if (expanded === id) setExpanded(null)
    setPanels((ps) => ps.filter((p) => p.id !== id))
  }

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

  function addPanel(type, bx, by) {
    const spec = PANEL_TYPES[type]
    setPanels((ps) => [...ps, {
      id: `p${Date.now()}`, type,
      x: Math.max(8, bx ?? 60), y: Math.max(8, by ?? 60),
      w: spec.w, h: spec.h, z: ++zRef.current, state: {},
    }])
    setMenu(null)
  }

  function openMenu(e) {
    e.preventDefault()
    const r = boardRef.current.getBoundingClientRect()
    setMenu({
      x: Math.min(e.clientX, window.innerWidth - 280),
      y: Math.min(e.clientY, window.innerHeight - 320),
      bx: e.clientX - r.left + boardRef.current.scrollLeft,
      by: e.clientY - r.top + boardRef.current.scrollTop,
    })
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
        <span className="dim hint">right-click the board to add a panel · double-click a
          title bar to expand · esc collapses</span>
        <button className="ghost" onClick={(e) => setMenu({
          x: e.clientX - 120, y: e.clientY + 14, bx: 80, by: 60 })}>+ panel</button>
      </header>
      <div className="board" ref={boardRef} onContextMenu={openMenu}>
        {panels.map((p) => (
          <Window key={p.id} panel={p}
                  expanded={expanded === p.id} expandRect={expandRect}
                  dimmed={expanded !== null && expanded !== p.id}
                  onPatch={(patch) => patchPanel(p.id, patch)}
                  onFront={() => front(p.id)}
                  onClose={() => close(p.id)}
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
    case 'journal': return <JournalPanel {...props} />
    case 'editor': return <EditorPanel {...props} />
    case 'renderer': return <RendererPanel {...props} />
    case 'organizer': return <OrganizerPanel {...props} />
    case 'run': return <RunPanel {...props} />
    case 'todos': return <TodoPanel {...props} />
    default: return <div className="dim">unknown panel</div>
  }
}

// ---- window chrome ----------------------------------------------------------

function Window({ panel, expanded, expandRect, dimmed, onPatch, onFront,
                  onClose, onToggleExpand, children }) {
  const [interacting, setInteracting] = useState(false)

  function track(e, apply) {
    e.preventDefault()
    onFront()
    setInteracting(true)
    const sx = e.clientX, sy = e.clientY
    const move = (ev) => apply(ev.clientX - sx, ev.clientY - sy)
    const up = () => {
      setInteracting(false)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const startDrag = (e) => {
    if (expanded || e.target.closest('button')) return
    const { x, y } = panel
    track(e, (dx, dy) => onPatch({ x: Math.max(0, x + dx), y: Math.max(0, y + dy) }))
  }
  const startResize = (e) => {
    if (expanded) return
    const { w, h } = panel
    track(e, (dx, dy) => onPatch({ w: Math.max(280, w + dx), h: Math.max(200, h + dy) }))
  }

  const style = expanded && expandRect
    ? { left: expandRect.x, top: expandRect.y, width: expandRect.w,
        height: expandRect.h, zIndex: 999 }
    : { left: panel.x, top: panel.y, width: panel.w, height: panel.h,
        zIndex: panel.z || 1 }

  return (
    <section className={`window ${interacting ? '' : 'anim'} ${expanded ? 'expanded' : ''} ${dimmed ? 'dimmed' : ''}`}
             style={style} onPointerDown={onFront}>
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
  const path = state.path || ''

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
        <button onClick={save} disabled={!dirty || !path}>{dirty ? 'Save' : 'Saved'}</button>
      </div>
      {binary
        ? <div className="dim center-pad">binary file</div>
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
