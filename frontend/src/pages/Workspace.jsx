import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../api.js'

// The workspace is a panel registry: to add a capability (finance sheet,
// homework helper, whatever), write a component taking {slug} and list it here.
const PANELS = [
  { id: 'overview', label: 'Overview', component: OverviewPanel },
  { id: 'files', label: 'Files', component: FilesPanel },
  { id: 'run', label: 'Run', component: RunPanel },
  { id: 'todos', label: 'To-dos', component: TodoPanel },
]

export default function Workspace() {
  const { slug } = useParams()
  const [project, setProject] = useState(null)
  const [tab, setTab] = useState('overview')

  const refresh = useCallback(
    () => api(`/api/projects/${slug}`).then(setProject), [slug])
  useEffect(() => { refresh() }, [refresh])

  if (!project) return <div className="center">…</div>
  const Panel = PANELS.find((p) => p.id === tab).component

  return (
    <div className="workspace">
      <header className="ws-head">
        <h2>{project.name}</h2>
        {project.loaded
          ? <button className="ghost" onClick={async () => {
              await api('/api/projects/unload', { method: 'POST' }); refresh() }}>
              in context ✓ (unload)
            </button>
          : <button className="ghost" onClick={async () => {
              await api(`/api/projects/${slug}/load`, { method: 'POST' }); refresh() }}>
              load into context
            </button>}
        <div className="tabs">
          {PANELS.map((p) => (
            <button key={p.id} className={`tab ${tab === p.id ? 'active' : ''}`}
                    onClick={() => setTab(p.id)}>{p.label}</button>
          ))}
        </div>
      </header>
      <Panel slug={slug} project={project} refreshProject={refresh} />
    </div>
  )
}

function OverviewPanel({ slug, project, refreshProject }) {
  const [md, setMd] = useState(project.project_md)
  const [dirty, setDirty] = useState(false)

  async function save() {
    await api(`/api/projects/${slug}/md`, {
      method: 'PUT', body: JSON.stringify({ content: md }) })
    setDirty(false)
    refreshProject()
  }

  return (
    <div className="panel">
      <p className="dim">project.md — the journal Jarvis loads with this project.
        The Summary section feeds all-projects.md.</p>
      <textarea className="md-editor grow" value={md}
                onChange={(e) => { setMd(e.target.value); setDirty(true) }} />
      <div className="row">
        <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
      </div>
    </div>
  )
}

const TEXT_EXT = /\.(md|txt|py|js|jsx|ts|json|html|css|csv|toml|yaml|yml|sh|tex)$/i
const IMG_EXT = /\.(png|jpg|jpeg|gif|svg|webp)$/i

function FilesPanel({ slug }) {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState(null)
  const [content, setContent] = useState('')
  const [binary, setBinary] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [mode, setMode] = useState('edit') // edit | preview
  const uploadRef = useRef(null)

  const refresh = useCallback(
    () => api(`/api/projects/${slug}/files`).then((r) => setFiles(r.files)), [slug])
  useEffect(() => { refresh() }, [refresh])

  async function open(path) {
    setSelected(path)
    setMode(IMG_EXT.test(path) || path.endsWith('.pdf') ? 'preview' : 'edit')
    const r = await api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`)
    setBinary(r.binary)
    setContent(r.binary ? '' : r.content)
    setDirty(false)
  }

  async function save() {
    await api(`/api/projects/${slug}/file`, {
      method: 'PUT', body: JSON.stringify({ path: selected, content }) })
    setDirty(false)
    refresh()
  }

  async function newFile() {
    const path = window.prompt('new file path (e.g. notes/plan.md, code/sim.py)')
    if (!path) return
    await api(`/api/projects/${slug}/file`, {
      method: 'PUT', body: JSON.stringify({ path, content: '' }) })
    await refresh()
    open(path)
  }

  async function del() {
    if (!selected || !window.confirm(`delete ${selected}?`)) return
    await api(`/api/projects/${slug}/file?path=${encodeURIComponent(selected)}`,
              { method: 'DELETE' })
    setSelected(null)
    refresh()
  }

  async function upload(e) {
    const file = e.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    await fetch(`/api/projects/${slug}/upload`, { method: 'POST', body: form })
    e.target.value = ''
    refresh()
  }

  const rawUrl = selected &&
    `/api/projects/${slug}/raw/${selected.split('/').map(encodeURIComponent).join('/')}`
  const isHtml = selected?.toLowerCase().endsWith('.html')

  return (
    <div className="panel split">
      <aside>
        <div className="row">
          <button className="ghost" onClick={newFile}>+ file</button>
          <button className="ghost" onClick={() => uploadRef.current.click()}>upload</button>
          <input ref={uploadRef} type="file" hidden onChange={upload} />
        </div>
        <ul className="file-list">
          {files.map((f) => (
            <li key={f.path} className={selected === f.path ? 'active' : ''}
                onClick={() => open(f.path)}>{f.path}</li>
          ))}
        </ul>
      </aside>
      <main className="editor-pane">
        {!selected ? (
          <div className="dim center-pad">select a file — or create one. Text is editable;
            HTML gets a sandboxed preview; images and PDFs render inline.</div>
        ) : (
          <>
            <div className="pane-head">
              <h3>{selected}</h3>
              {isHtml && (
                <button className="ghost" onClick={() => setMode(mode === 'edit' ? 'preview' : 'edit')}>
                  {mode === 'edit' ? 'preview' : 'edit'}
                </button>
              )}
              <a className="ghost-link" href={rawUrl} target="_blank" rel="noreferrer">open raw</a>
              <button className="ghost danger" onClick={del}>delete</button>
              {!binary && mode === 'edit' && (
                <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
              )}
            </div>
            {mode === 'preview' && isHtml ? (
              <iframe className="preview-frame" sandbox="allow-scripts"
                      title="preview" srcDoc={content} />
            ) : mode === 'preview' && IMG_EXT.test(selected) ? (
              <div className="preview-scroll"><img src={rawUrl} alt={selected} /></div>
            ) : mode === 'preview' && selected.endsWith('.pdf') ? (
              <embed className="preview-frame" src={rawUrl} type="application/pdf" />
            ) : binary ? (
              <div className="dim center-pad">binary file — use “open raw”</div>
            ) : (
              <textarea className="md-editor grow" spellCheck={false} value={content}
                        onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
            )}
          </>
        )}
      </main>
    </div>
  )
}

function RunPanel({ slug }) {
  const [code, setCode] = useState(
    '# scratch pad — runs on the Pi (light sandbox)\n' +
    '# numpy / matplotlib / sympy / pandas / reportlab available\n' +
    'print("hello from jarvis workspace")\n')
  const [pyFiles, setPyFiles] = useState([])
  const [runFile, setRunFile] = useState('')
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)

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

  const rawUrl = (p) =>
    `/api/projects/${slug}/raw/${p.split('/').map(encodeURIComponent).join('/')}`

  return (
    <div className="panel">
      <textarea className="md-editor code" spellCheck={false} rows={12}
                value={code} onChange={(e) => setCode(e.target.value)} />
      <div className="row">
        <button onClick={() => run({ code })} disabled={busy}>
          {busy ? 'running…' : '▶ Run scratch'}
        </button>
        <select value={runFile} onChange={(e) => setRunFile(e.target.value)}>
          <option value="">— or pick a .py file —</option>
          {pyFiles.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <button className="ghost" disabled={!runFile || busy}
                onClick={() => run({ path: runFile })}>▶ Run file</button>
      </div>
      {result && (
        <div className="run-result">
          <div className="row dim">
            exit {result.exit_code} · {result.duration}s
            {result.timed_out && <span className="warn"> · timed out</span>}
          </div>
          {result.stdout && <pre className="console">{result.stdout}</pre>}
          {result.stderr && <pre className="console err">{result.stderr}</pre>}
          {result.artifacts?.length > 0 && (
            <div className="artifacts">
              <div className="side-title">artifacts</div>
              {result.artifacts.map((a) => (
                <div key={a} className="artifact">
                  <a href={rawUrl(a)} target="_blank" rel="noreferrer">{a}</a>
                  {IMG_EXT.test(a) && <img src={`${rawUrl(a)}?t=${Date.now()}`} alt={a} />}
                </div>
              ))}
            </div>
          )}
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
    <div className="panel narrow">
      <form className="row" onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) { act({ action: 'add', text }); setText('') }
      }}>
        <input className="grow" placeholder="add a to-do… (stored in todo.md)"
               value={text} onChange={(e) => setText(e.target.value)} />
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
            <button className="ghost danger"
                    onClick={() => act({ action: 'delete', index: i })}>×</button>
          </li>
        ))}
        {todos.length === 0 && <li className="dim">nothing yet</li>}
      </ul>
    </div>
  )
}
