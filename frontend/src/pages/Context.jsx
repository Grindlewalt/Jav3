import { useEffect, useState } from 'react'
import { api } from '../api.js'

const ASSEMBLED = '::assembled'

export default function Context() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState('soul.md')
  const [content, setContent] = useState('')
  const [assembled, setAssembled] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState('')
  const [secrets, setSecrets] = useState([])

  async function refresh() {
    const r = await api('/api/memory')
    setFiles(r.files)
    api('/api/secrets').then((s) => setSecrets(s.secrets)).catch(() => {})
  }
  useEffect(() => { refresh() }, [])

  async function addSecret() {
    const name = window.prompt('secret name (e.g. TBA_KEY)')
    if (!name) return
    const value = window.prompt(`value for ${name.toUpperCase()} (stored host-side; the agent only ever sees the name)`)
    if (!value) return
    try {
      await api(`/api/secrets/${encodeURIComponent(name)}`, {
        method: 'PUT', body: JSON.stringify({ value }) })
      refresh()
    } catch (err) { window.alert(err.detail || String(err)) }
  }

  async function delSecret(name) {
    if (!window.confirm(`delete secret ${name}?`)) return
    await api(`/api/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
    refresh()
  }

  useEffect(() => {
    if (selected === ASSEMBLED) {
      api('/api/debug/context').then(setAssembled)
    } else {
      api(`/api/memory/file?path=${encodeURIComponent(selected)}`)
        .then((r) => { setContent(r.binary ? '(binary file)' : r.content); setDirty(false) })
    }
  }, [selected])

  const meta = files.find((f) => f.path === selected)
  const readOnly = selected === ASSEMBLED

  async function save() {
    await api('/api/memory/file', {
      method: 'PUT',
      body: JSON.stringify({ path: selected, content }),
    })
    setDirty(false)
    setStatus('saved')
    setTimeout(() => setStatus(''), 1500)
    refresh()
  }

  async function newNote() {
    const name = window.prompt('note name (e.g. ideas)')
    if (!name) return
    const path = `notes/${name.replace(/\.md$/, '')}.md`
    await api('/api/memory/file', {
      method: 'PUT',
      body: JSON.stringify({ path, content: `# ${name}\n\n` }),
    })
    await refresh()
    setSelected(path)
  }

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Jarvis's memory</div>
        <ul className="file-list">
          <li className={selected === ASSEMBLED ? 'active' : ''}
              onClick={() => setSelected(ASSEMBLED)}>
            ⚡ assembled context (live)
          </li>
          {files.map((f) => (
            <li key={f.path} className={selected === f.path ? 'active' : ''}
                onClick={() => setSelected(f.path)}>
              <span className="grow">{f.path}</span>
              {f.auto_generated && <span className="tag">auto</span>}
              {f.tokens != null && <span className="dim small">≈{f.tokens.toLocaleString()} tok</span>}
            </li>
          ))}
        </ul>
        <button className="ghost" onClick={newNote}>+ new note</button>
        <div className="side-title" style={{ marginTop: 16 }}
             title="API keys the agent can use via {{secret:NAME}} in VM runs but never read">
          Secrets</div>
        <ul className="file-list">
          {secrets.length === 0 && <li className="dim">none saved</li>}
          {secrets.map((s) => (
            <li key={s.name}>
              <span className="grow">{s.name}</span>
              <span className="dim small">…{s.last4}</span>
              <button className="win-btn" title="delete"
                      onClick={() => delSecret(s.name)}>×</button>
            </li>
          ))}
        </ul>
        <button className="ghost" onClick={addSecret}>+ add secret</button>
      </aside>
      <main className="editor-pane">
        {readOnly ? (
          <>
            <div className="pane-head">
              <h3>What Jarvis sees right now</h3>
              <span className="dim">
                {assembled?.active_project
                  ? `project loaded: ${assembled.active_project}`
                  : 'no project loaded'}
                {assembled?.tokens != null &&
                  ` · ≈${assembled.tokens.toLocaleString()} input tokens ride every turn`}
              </span>
            </div>
            <pre className="context-view">{assembled?.system_prompt || '…'}</pre>
          </>
        ) : (
          <>
            <div className="pane-head">
              <h3>{selected}</h3>
              {meta?.auto_generated && (
                <span className="warn">regenerated from project summaries — edits will be overwritten</span>
              )}
              <span className="dim">{status}</span>
              <button onClick={save} disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            <textarea className="md-editor grow" value={content}
                      onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
          </>
        )}
      </main>
    </div>
  )
}
