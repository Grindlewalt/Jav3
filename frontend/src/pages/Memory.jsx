import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import Page from '../components/Page.jsx'
import Tag from '../components/Tag.jsx'

const ASSEMBLED = '::assembled'
const QUEUE = '::review'
const TRASH = '::trash'
const SPECIAL = [ASSEMBLED, QUEUE, TRASH]

// A note file path (notes/foo.md) and the notes-API `name` field don't share a
// spelling, so key both by the bare stem to line trust metadata up with files.
const nkey = (p) => String(p || '').replace(/^notes\//, '').replace(/\.md$/, '')
const isNote = (p) => String(p || '').startsWith('notes/')

// What the agent may call a note (tools/memory_write): lowercase letters,
// digits and hyphens. A note the operator names "My Ideas" would be one the
// agent could not open, so the page makes the same name.
const slug = (s) => String(s || '').trim().toLowerCase().replace(/\.md$/, '')
  .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80)

// The note changed on the server: the nav badge counts it, so tell it to look.
const changed = () => window.dispatchEvent(new Event('jarvis-memory-changed'))

const when = (iso) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? String(iso || '') : d.toLocaleString()
}

// Why a note is waiting, in the operator's words. `taint` is set when the turn
// that wrote it had read something from outside (a web page, a screen, files
// from a computer, another agent's message): not always the web.
function whyPending(n) {
  if (n.bad_frontmatter) {
    return 'Its header could not be read, so it is held back. Approving rewrites the header.'
  }
  if (n.taint === 'untrusted') {
    return 'The turn that wrote this had read outside content (a web page, a screen, files or '
      + "another agent's message). Read the text before you approve it."
  }
  return 'Written by Jav3. It is not in its context or rules until you approve it.'
}

function DiffView({ diff, body }) {
  if (!diff) {
    return body
      ? <pre className="mem-body">{body}</pre>
      : <p className="dim">No change in the text.</p>
  }
  return (
    <pre className="mem-diff">
      {diff.split('\n').map((ln, i) => {
        if (ln.startsWith('---') || ln.startsWith('+++')) return null
        const cls = ln.startsWith('@@') ? 'hunk' : ln.startsWith('+') ? 'add'
          : ln.startsWith('-') ? 'del' : ''
        return <span key={i} className={`mem-dl ${cls}`}>{ln || ' '}</span>
      })}
    </pre>
  )
}

function ProposalCard({ p, busy, onApprove, onReject, onOpen }) {
  return (
    <section className="mem-card">
      <div className="mem-card-head">
        <h3><span className="dim">change to </span>{p.name}</h3>
        {p.taint === 'untrusted' && (
          <Tag tone="untrusted"
               title="The turn that wrote this had read outside content. Read the diff before approving.">
            read outside content</Tag>)}
        {p.stale && <Tag tone="pending" title="The note was edited after this proposal began">
          note edited since</Tag>}
      </div>
      <p className="mem-why">
        Jav3 wants to change a note that is already binding. The note is unchanged and still
        in force until you approve.
      </p>
      {p.description && <p className="mem-desc">{p.description}</p>}
      <DiffView diff={p.diff} body={p.base_exists ? '' : p.body} />
      {p.stale && (
        <p className="warn">You edited this note after the proposal began. Approving replaces
          your edit with the proposal's text.</p>)}
      <div className="mem-actions">
        <button disabled={busy} onClick={() => onApprove(p)}>
          {p.stale ? 'Approve anyway' : 'Approve'}</button>
        <button className="ghost" disabled={busy} onClick={() => onReject(p)}>Reject</button>
        <button className="ghost" onClick={() => onOpen(p.name)}>Open note</button>
      </div>
    </section>
  )
}

function NoteCard({ n, busy, onApprove, onReject, onOpen }) {
  return (
    <section className="mem-card">
      <div className="mem-card-head">
        <h3>{n.name}</h3>
        <Tag tone="pending">{n.source === 'agent' ? 'agent note' : 'held back'}</Tag>
        {n.taint === 'untrusted' && !n.bad_frontmatter && (
          <Tag tone="untrusted" title="Read the text before you approve it">
            read outside content</Tag>)}
        {n.bad_frontmatter && <Tag tone="untrusted">unreadable header</Tag>}
        <span className="dim mem-when">{when(n.mtime * 1000)}</span>
      </div>
      {n.description && <p className="mem-desc">{n.description}</p>}
      <p className="mem-why">{whyPending(n)}</p>
      <pre className="mem-body">{n.body || '(empty)'}</pre>
      <div className="mem-actions">
        <button disabled={busy} onClick={() => onApprove(n)}
                title="Makes this note binding: it rides Jav3's context on every turn">
          Approve</button>
        <button className="ghost danger" disabled={busy} onClick={() => onReject(n)}
                title="Moves the note to the Trash; you can restore it">Reject</button>
        <button className="ghost" onClick={() => onOpen(n.name)}>Open</button>
      </div>
    </section>
  )
}

export default function Memory() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState('soul.md')
  const [content, setContent] = useState('')
  const [fileSha, setFileSha] = useState('')
  const [loadError, setLoadError] = useState('')
  const [assembled, setAssembled] = useState(null)
  const [dirty, setDirty] = useState(false)
  const [status, setStatus] = useState('')
  const [conflict, setConflict] = useState(false)
  const [notes, setNotes] = useState({})   // stem -> the notes-API row
  const [proposals, setProposals] = useState([])
  const [trash, setTrash] = useState([])
  const [busy, setBusy] = useState('')
  const picked = useRef(false)              // the operator chose a view: don't move them
  const ask = useAsk()

  const refresh = useCallback(async () => {
    const r = await api('/api/memory')
    // a dotfile (.gitkeep) is plumbing that keeps a folder in version control, not memory
    setFiles(r.files.filter((f) => !f.path.split('/').pop().startsWith('.')))
    // the queue's three lists: an older server without them shows an empty one
    const [n, p, t] = await Promise.all([
      api('/api/memory/notes').catch(() => ({ notes: [] })),
      api('/api/memory/proposals').catch(() => ({ items: [] })),
      api('/api/memory/trash').catch(() => ({ items: [] })),
    ])
    const m = {}; (n.notes || []).forEach((row) => { m[nkey(row.name)] = row })
    setNotes(m)
    setProposals(p.items || [])
    setTrash(t.items || [])
    return { notes: m, proposals: p.items || [] }
  }, [])

  // first load: land on the queue when something waits, on soul.md otherwise
  useEffect(() => {
    refresh().then(({ notes: m, proposals: p }) => {
      if (!picked.current && (p.length || Object.values(m).some((x) => x.pending))) {
        setSelected(QUEUE)
      }
    }).catch(notifyError)
  }, [refresh])

  // the agent may have written since this tab was last looked at
  useEffect(() => {
    const onFocus = () => { refresh().catch(() => {}) }
    window.addEventListener('focus', onFocus)
    window.addEventListener('jarvis-memory-arrived', onFocus)   // Notices: a new pending note
    return () => {
      window.removeEventListener('focus', onFocus)
      window.removeEventListener('jarvis-memory-arrived', onFocus)
    }
  }, [refresh])

  const loadFile = useCallback((path) => {
    setLoadError('')
    return api(`/api/memory/file?path=${encodeURIComponent(path)}`)
      .then((r) => {
        setContent(r.binary ? '(binary file)' : r.content)
        setFileSha(r.sha256 || '')
        setDirty(false)
        setConflict(false)
      })
      .catch((err) => {
        // never leave the previous file's text under the new file's name
        setContent(''); setFileSha(''); setDirty(false)
        setLoadError(err.detail || String(err))
      })
  }, [])

  useEffect(() => {
    if (selected === ASSEMBLED) {
      api('/api/debug/context').then(setAssembled).catch(notifyError)
    } else if (!SPECIAL.includes(selected)) {
      loadFile(selected)
    }
  }, [selected, loadFile])

  async function pick(path) {
    if (path === selected) return
    if (dirty && !await ask.confirm('Discard your unsaved changes to this file?')) return
    picked.current = true
    setStatus('')
    setSelected(path)
  }

  const meta = files.find((f) => f.path === selected)
  const noteMeta = isNote(selected) ? notes[nkey(selected)] : undefined
  const special = SPECIAL.includes(selected)
  const pendingNotes = Object.values(notes).filter((n) => n.pending)
    .sort((a, b) => b.mtime - a.mtime)
  const waiting = pendingNotes.length + proposals.length
  // the agent (or a scheduled run) wrote to this note after the editor loaded it
  const editedElsewhere = !!noteMeta && !!fileSha && !dirty && noteMeta.sha256 !== fileSha

  async function act(key, fn, done) {
    setBusy(key)
    try {
      await fn()
      if (done) notify(done, { sev: 'ok', life: 4 })
    } catch (err) {
      notifyError(err)
    } finally {
      // whatever happened, show what is on the server now
      await refresh().catch(() => {})
      changed()
      setBusy('')
    }
  }

  const approveNote = (n) => act(`n:${n.name}`, () => api(
    `/api/memory/notes/${encodeURIComponent(n.name)}/promote`,
    { method: 'POST', body: JSON.stringify({ sha256: n.sha256 }) }), `Approved ${n.name}`)
  const rejectNote = (n) => act(`n:${n.name}`, () => api(
    `/api/memory/notes/${encodeURIComponent(n.name)}`, { method: 'DELETE' }),
  `${n.name} moved to the Trash`)
  const approveProposal = (p) => act(`p:${p.name}`, () => api(
    `/api/memory/proposals/${encodeURIComponent(p.name)}/approve`,
    { method: 'POST', body: JSON.stringify({ sha256: p.sha256, force: !!p.stale }) }),
  `Applied the change to ${p.name}`)
  const rejectProposal = (p) => act(`p:${p.name}`, () => api(
    `/api/memory/proposals/${encodeURIComponent(p.name)}/reject`, { method: 'POST' }),
  `Rejected the change to ${p.name}`)
  const restore = (t) => act(`t:${t.id}`, () => api(
    `/api/memory/trash/${encodeURIComponent(t.id)}/restore`, { method: 'POST' }),
  `Restored ${t.name}`)
  const openNote = (name) => pick(`notes/${name}.md`)

  // approve from the editor: bound to the text on screen. Unsaved edits would
  // not be the text that gets approved, so save first.
  async function promoteOpen() {
    setBusy('editor')
    try {
      await api(`/api/memory/notes/${encodeURIComponent(noteMeta.name)}/promote`, {
        method: 'POST', body: JSON.stringify({ sha256: fileSha }) })
      // the file was rewritten (approved: true, no taint): show it, not the stale text
      await loadFile(selected)
      notify(`Approved ${noteMeta.name}`, { sev: 'ok', life: 4 })
    } catch (err) {
      notifyError(err)
    } finally {
      await refresh().catch(() => {})
      changed()
      setBusy('')
    }
  }

  async function save() {
    try {
      const r = await api('/api/memory/file', {
        method: 'PUT',
        body: JSON.stringify({ path: selected, content, if_sha256: fileSha || undefined }),
      })
      setFileSha(r.sha256 || '')
      setDirty(false)
      setConflict(false)
      setStatus('saved')
      setTimeout(() => setStatus(''), 1500)
      refresh().catch(() => {})
      changed()
    } catch (err) {
      if (err.status === 409) setConflict(true)
      else notifyError(err)
    }
  }

  async function deleteOpen() {
    if (!noteMeta) return
    await act('editor', async () => {
      await api(`/api/memory/notes/${encodeURIComponent(noteMeta.name)}`, { method: 'DELETE' })
      picked.current = true
      setDirty(false)
      setSelected(TRASH)
    }, `${noteMeta.name} moved to the Trash`)
  }

  async function newNote() {
    const raw = await ask.prompt('Note name', '',
                                 { placeholder: 'e.g. ideas', confirmLabel: 'Create' })
    if (!raw) return
    const name = slug(raw)
    if (!name) { notify('Use letters or digits in the note name', { sev: 'warn' }); return }
    const path = `notes/${name}.md`
    if (files.some((f) => f.path === path)) {
      notify(`${name} already exists; opened it`, { sev: 'warn', life: 5 })
      pick(path)
      return
    }
    try {
      await api('/api/memory/file', {
        method: 'PUT',
        body: JSON.stringify({ path, content: `# ${name}\n\n`, create_only: true }),
      })
    } catch (err) { notifyError(err); return }
    await refresh().catch(() => {})
    picked.current = true
    setSelected(path)
  }

  return (
    <Page variant="split" title="Memory">
      <aside className="mem-aside">
        <ul className="file-list mem-list">
          <li className={selected === QUEUE ? 'active mem-live' : 'mem-live'}
              onClick={() => pick(QUEUE)}>
            <span className="mem-name">waiting for you</span>
            <span className="mem-meta">
              {waiting > 0
                ? <Tag tone="pending">{waiting} to review</Tag>
                : <span className="dim">nothing waiting</span>}
            </span>
          </li>
          <li className={selected === ASSEMBLED ? 'active mem-live' : 'mem-live'}
              onClick={() => pick(ASSEMBLED)}>
            <span className="mem-name">assembled context</span>
            <span className="mem-meta dim">live — what rides every turn</span>
          </li>
          {files.map((f) => {
            const nm = notes[nkey(f.path)]
            const isN = isNote(f.path) && nm
            const hasProposal = isN && nm.proposal
            const rides = !/\.bak/.test(f.path) && !(isN && nm.pending)
            return (
              <li key={f.path} className={selected === f.path ? 'active' : ''}
                  title={f.path} onClick={() => pick(f.path)}>
                <span className="mem-name">{f.path}</span>
                {isN && nm.description && (
                  <span className="mem-blurb dim">{nm.description}</span>)}
                {(isN && nm.pending) || hasProposal || f.auto_generated
                  || (rides && f.tokens != null) ? (
                    <span className="mem-meta">
                      {isN && nm.pending && (
                        <Tag tone="pending" title="Written by Jav3, waiting for your approval. Not in its context or rules.">
                          pending</Tag>)}
                      {isN && nm.pending && nm.taint === 'untrusted' && (
                        <Tag tone="untrusted" title="The turn that wrote it had read outside content">
                          outside content</Tag>)}
                      {hasProposal && (
                        <Tag tone="pending" title="Jav3 proposed a change to this note; it is unchanged until you approve">
                          change proposed</Tag>)}
                      {f.auto_generated && <Tag>auto</Tag>}
                      {rides && f.tokens != null && (
                        <span className="dim" title="Rough input tokens if the whole file rides the context">
                          ≈{f.tokens.toLocaleString()} tok</span>)}
                    </span>
                  ) : null}
              </li>
            )
          })}
          <li className={selected === TRASH ? 'active mem-live' : 'mem-live'}
              onClick={() => pick(TRASH)}>
            <span className="mem-name">trash</span>
            <span className="mem-meta dim">
              {trash.length ? `${trash.length} deleted, restorable` : 'empty'}</span>
          </li>
        </ul>
        <button className="ghost" onClick={newNote}>+ new note</button>
        <details className="mem-legend">
          <summary>What the tags mean</summary>
          <dl>
            <dt><Tag tone="pending">pending</Tag></dt>
            <dd>Jav3 wrote this note and it waits for you. It is not in its context, index or
              rules until you approve it.</dd>
            <dt><Tag tone="untrusted">outside content</Tag></dt>
            <dd>The turn that wrote it had read a web page, a screen, files or another agent's
              message. Read it before you approve.</dd>
            <dt><Tag tone="pending">change proposed</Tag></dt>
            <dd>Jav3 wants to change a note that is already binding. The note stays as it is
              until you approve the change.</dd>
            <dt><Tag>auto</Tag></dt>
            <dd>Regenerated from project summaries; your edits are overwritten.</dd>
            <dt><span className="dim">≈tok</span></dt>
            <dd>Rough input tokens if the whole file rides the prompt. Notes past the first
              couple thousand tokens are listed by name only.</dd>
          </dl>
        </details>
      </aside>
      <main className="editor-pane">
        {selected === ASSEMBLED && (
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
        )}
        {selected === QUEUE && (
          <>
            <div className="pane-head">
              <h3>Waiting for you</h3>
              <span className="dim">
                {waiting
                  ? `${pendingNotes.length} note${pendingNotes.length === 1 ? '' : 's'}, `
                    + `${proposals.length} proposed change${proposals.length === 1 ? '' : 's'}`
                  : 'nothing to review'}</span>
            </div>
            <div className="mem-queue">
              <p className="dim mem-intro">
                What Jav3 saves stays here until you approve it: it is not in its context and
                not one of its rules before then. Notes you write yourself are trusted at once.
              </p>
              {proposals.map((p) => (
                <ProposalCard key={`p:${p.name}`} p={p} busy={busy === `p:${p.name}`}
                              onApprove={approveProposal} onReject={rejectProposal}
                              onOpen={openNote} />))}
              {pendingNotes.map((n) => (
                <NoteCard key={`n:${n.name}`} n={n} busy={busy === `n:${n.name}`}
                          onApprove={approveNote} onReject={rejectNote} onOpen={openNote} />))}
              {!waiting && (
                <p className="mem-empty">All clear. A note Jav3 saves will show up here with
                  its text, ready to approve or reject.</p>)}
            </div>
          </>
        )}
        {selected === TRASH && (
          <>
            <div className="pane-head">
              <h3>Trash</h3>
              <span className="dim">deleted notes stay here until restored</span>
            </div>
            <div className="mem-queue">
              {trash.map((t) => (
                <section className="mem-card mem-trash" key={t.id}>
                  <div className="mem-card-head">
                    <h3>{t.name}</h3>
                    {t.source === 'agent' && <Tag>agent note</Tag>}
                    {t.taint === 'untrusted' && <Tag tone="untrusted">outside content</Tag>}
                    {t.has_proposal && <Tag tone="pending">had a proposed change</Tag>}
                    <span className="dim mem-when">
                      deleted {when(t.deleted_at)} · {t.size.toLocaleString()} B</span>
                    <button className="ghost" disabled={busy === `t:${t.id}`}
                            onClick={() => restore(t)}>Restore</button>
                  </div>
                </section>))}
              {!trash.length && (
                <p className="mem-empty">The trash is empty. A deleted note waits here so a
                  mistake can be undone.</p>)}
            </div>
          </>
        )}
        {!special && (
          <>
            <div className="pane-head">
              <h3>{selected}</h3>
              {noteMeta?.taint === 'untrusted' && (
                <span className="warn"
                      title="The turn that wrote it had read a web page, a screen, files or another agent's message">
                  written after reading outside content</span>)}
              {noteMeta?.pending && <span className="tag pending">pending approval</span>}
              {meta?.auto_generated && (
                <span className="warn">regenerated from project summaries — edits will be overwritten</span>
              )}
              <span className="dim">{status}</span>
              {noteMeta?.pending && (
                <button className="ghost" disabled={dirty || busy === 'editor' || !fileSha}
                        title={dirty ? 'Save your edits first: approving applies to the saved text'
                          : 'Approve this note: it becomes binding and rides every turn'}
                        onClick={promoteOpen}>Trust and approve</button>)}
              {noteMeta && (
                <button className="ghost danger" disabled={busy === 'editor'}
                        title="Moves the note to the Trash; you can restore it"
                        onClick={deleteOpen}>Delete</button>)}
              <button onClick={save} disabled={!dirty || !!loadError}>{dirty ? 'Save' : 'Saved'}</button>
            </div>
            {noteMeta?.proposal && (
              <p className="mem-banner">Jav3 proposed a change to this note. It is unchanged until
                you approve it. <button className="link" onClick={() => pick(QUEUE)}>Review the change</button></p>)}
            {(conflict || editedElsewhere) && (
              <p className="mem-banner warn">
                {conflict
                  ? 'This file changed since you opened it, so nothing was saved. '
                  : 'This note changed since you opened it (Jav3 or a scheduled run wrote to it). '}
                <button className="link" onClick={() => loadFile(selected)}>
                  Reload{dirty ? ' (drops your edits)' : ''}</button></p>)}
            {loadError ? (
              <p className="mem-banner warn">Could not open {selected}: {loadError}</p>
            ) : (
              <textarea className="md-editor grow" value={content}
                        onChange={(e) => { setContent(e.target.value); setDirty(true) }} />
            )}
          </>
        )}
      </main>
    </Page>
  )
}
