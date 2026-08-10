import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'

const ASSEMBLED = '::assembled'

// A note file path (notes/foo.md) and the notes-API `name` field don't share a
// spelling, so key both by the bare stem to line trust metadata up with files.
const nkey = (p) => String(p || '').replace(/^notes\//, '').replace(/\.md$/, '')

export default function Context() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState('soul.md')
  const [content, setContent] = useState('')
  const [assembled, setAssembled] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState('')
  const [secrets, setSecrets] = useState([])
  const [notes, setNotes] = useState({})   // stem -> {name,source,approved,taint,trusted}

  async function refresh() {
    const r = await api('/api/memory')
    setFiles(r.files)
    api('/api/secrets').then((s) => setSecrets(s.secrets)).catch(() => {})
    api('/api/memory/notes').then((r2) => {
      const m = {}; (r2.notes || []).forEach((n) => { m[nkey(n.name)] = n })
      setNotes(m)
    }).catch(() => {})
  }
  useEffect(() => { refresh() }, [])

  async function promote(name) {
    try {
      await api(`/api/memory/notes/${encodeURIComponent(name)}/promote`, { method: 'POST' })
      await refresh()
    } catch (err) { notifyError(err) }
  }

  async function addSecret() {
    const name = window.prompt('secret name (e.g. TBA_KEY)')
    if (!name) return
    const value = window.prompt(`value for ${name.toUpperCase()} (stored host-side; the agent only ever sees the name)`)
    if (!value) return
    const hostsRaw = window.prompt(
      'web hosts this key may be sent to, comma-separated (e.g. newsapi.org).\n'
      + 'Leave empty to keep it unusable — web_read refuses unbound keys.') || ''
    const hosts = hostsRaw.split(',').map((h) => h.trim()).filter(Boolean)
    try {
      await api(`/api/secrets/${encodeURIComponent(name)}`, {
        method: 'PUT', body: JSON.stringify({ value, hosts }) })
      refresh()
    } catch (err) { notifyError(err) }
  }

  async function editHosts(s) {
    const hostsRaw = window.prompt(
      `web hosts ${s.name} may be sent to (comma-separated; empty = unusable)`,
      (s.hosts || []).join(', '))
    if (hostsRaw === null) return
    const hosts = hostsRaw.split(',').map((h) => h.trim()).filter(Boolean)
    try {
      await api(`/api/secrets/${encodeURIComponent(s.name)}`, {
        method: 'PUT', body: JSON.stringify({ value: '', hosts }) })
      refresh()
    } catch (err) { notifyError(err) }
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
  const noteMeta = notes[nkey(selected)]
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
          {files.map((f) => {
            const nm = notes[nkey(f.path)]
            return (
              <li key={f.path} className={selected === f.path ? 'active' : ''}
                  onClick={() => setSelected(f.path)}>
                <span className="grow">{f.path}</span>
                {nm?.taint === 'untrusted' && (
                  <span className="tag untrusted" title="from web/research — untrusted">untrusted</span>)}
                {nm && nm.source === 'agent' && !nm.approved && (
                  <span className="tag pending" title="agent-created — pending approval">pending</span>)}
                {f.auto_generated && <span className="tag">auto</span>}
                {f.tokens != null && <span className="dim small">≈{f.tokens.toLocaleString()} tok</span>}
              </li>
            )
          })}
        </ul>
        <button className="ghost" onClick={newNote}>+ new note</button>
        <div className="side-title" style={{ marginTop: 16 }}
             title="API keys the agent can use via {{secret:NAME}} in web_read (on bound hosts) but never read">
          Secrets</div>
        <ul className="file-list">
          {secrets.length === 0 && <li className="dim">none saved</li>}
          {secrets.map((s) => (
            <li key={s.name} title={s.hosts?.length
                  ? `web: ${s.hosts.join(', ')}` : 'no web hosts bound — unusable'}>
              <span className="grow">{s.name}
                {s.hosts?.length > 0 && <span className="tag">web</span>}</span>
              <span className="dim small">…{s.last4}</span>
              <button className="win-btn" title="edit web hosts"
                      onClick={() => editHosts(s)}>✎</button>
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
              {noteMeta?.taint === 'untrusted' && (
                <span className="warn">untrusted — from web/research</span>)}
              {noteMeta && noteMeta.source === 'agent' && !noteMeta.approved && (
                <span className="tag pending">pending approval</span>)}
              {meta?.auto_generated && (
                <span className="warn">regenerated from project summaries — edits will be overwritten</span>
              )}
              <span className="dim">{status}</span>
              {noteMeta && !noteMeta.trusted && (
                <button className="ghost" title="mark this note trusted"
                        onClick={() => promote(noteMeta.name)}>Promote to trusted</button>)}
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
