import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import Page from '../components/Page.jsx'
import Tag from '../components/Tag.jsx'

const ASSEMBLED = '::assembled'

// A note file path (notes/foo.md) and the notes-API `name` field don't share a
// spelling, so key both by the bare stem to line trust metadata up with files.
const nkey = (p) => String(p || '').replace(/^notes\//, '').replace(/\.md$/, '')

export default function Memory() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState('soul.md')
  const [content, setContent] = useState('')
  const [assembled, setAssembled] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState('')
  const [notes, setNotes] = useState({})   // stem -> {name,source,approved,taint,trusted}
  const ask = useAsk()

  async function refresh() {
    const r = await api('/api/memory')
    // a dotfile (.gitkeep) is plumbing that keeps a folder in version control, not memory
    setFiles(r.files.filter((f) => !f.path.split('/').pop().startsWith('.')))
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
    const name = await ask.prompt('Note name', '',
                                  { placeholder: 'e.g. ideas', confirmLabel: 'Create' })
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
    <Page variant="split" title="Memory">
      <aside className="mem-aside">
        <ul className="file-list mem-list">
          <li className={selected === ASSEMBLED ? 'active mem-live' : 'mem-live'}
              onClick={() => setSelected(ASSEMBLED)}>
            <span className="mem-name">assembled context</span>
            <span className="mem-meta dim">live — what rides every turn</span>
          </li>
          {files.map((f) => {
            const nm = notes[nkey(f.path)]
            const untrusted = nm?.taint === 'untrusted'
            const pending = nm && nm.source === 'agent' && !nm.approved
            return (
              <li key={f.path} className={selected === f.path ? 'active' : ''}
                  title={f.path} onClick={() => setSelected(f.path)}>
                <span className="mem-name">{f.path}</span>
                {(untrusted || pending || f.auto_generated || f.tokens != null) && (
                  <span className="mem-meta">
                    {untrusted && (
                      <Tag tone="untrusted" title="from web/research — untrusted">untrusted</Tag>)}
                    {pending && (
                      <Tag tone="pending" title="agent-created — pending approval">pending</Tag>)}
                    {f.auto_generated && <Tag>auto</Tag>}
                    {f.tokens != null && <span className="dim">≈{f.tokens.toLocaleString()} tok</span>}
                  </span>
                )}
              </li>
            )
          })}
        </ul>
        <button className="ghost" onClick={newNote}>+ new note</button>
      </aside>
      <main className="editor-pane">
        {readOnly ? (
          <>
            <div className="pane-head">
              <h3>What Jav3 sees right now</h3>
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
    </Page>
  )
}
