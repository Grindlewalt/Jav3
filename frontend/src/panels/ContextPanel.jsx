import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// The project's ALWAYS-LOADED files: the ones whose full text rides every
// prompt. project.md always does (locked); tick any other file to add it. This
// list is the operator's alone: an agent cannot tick a file, and when Jav3
// writes to one of these after reading outside content the change waits on the
// Memory page until you approve it (backend/alwaysloaded.py). The token count
// and running total keep you honest about how big the context gets.
export default function ContextPanel({ slug }) {
  const [files, setFiles] = useState([])
  const [total, setTotal] = useState(0)
  const [held, setHeld] = useState(0)
  const [busy, setBusy] = useState(false)

  const refresh = () =>
    api(`/api/projects/${slug}/context`).then((r) => {
      setFiles(r.files)
      setTotal(r.selected_tokens)
      setHeld(r.held || 0)
    })
  useEffect(() => {
    refresh()
    const h = () => refresh()
    window.addEventListener('jarvis-files-changed', h)
    window.addEventListener('jarvis-memory-arrived', h)   // Notices: a change was just held
    return () => {
      window.removeEventListener('jarvis-files-changed', h)
      window.removeEventListener('jarvis-memory-arrived', h)
    }
  }, [slug]) // eslint-disable-line

  // project.md is always loaded and never stored in the list
  const ticked = (list) => list.filter((f) => f.selected && !f.locked).map((f) => f.path)

  async function toggle(path) {
    setBusy(true)
    const next = files.some((f) => f.path === path && f.selected)
      ? ticked(files).filter((p) => p !== path)
      : [...ticked(files), path]
    try {
      await api(`/api/projects/${slug}/context`, {
        method: 'PUT', body: JSON.stringify({ files: next }) })
      await refresh()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`)

  async function setAll(on) {
    setBusy(true)
    try {
      await api(`/api/projects/${slug}/context`, {
        method: 'PUT',
        body: JSON.stringify({
          files: on ? files.filter((f) => !f.binary && !f.locked).map((f) => f.path) : [] }) })
      await refresh()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  // project.md first: it is always in, whatever is ticked
  const rows = [...files.filter((f) => f.locked), ...files.filter((f) => !f.locked)]
  const projectMd = files.find((f) => f.locked)
  const loaded = total + (projectMd ? projectMd.tokens : 0)

  return (
    <div className="pane-col">
      <div className="row">
        <span className="grow dim">always loaded: project.md, plus what you tick</span>
        <button className="ghost" disabled={busy || files.length === 0}
                onClick={() => setAll(true)}>all</button>
        <button className="ghost" disabled={busy || files.length === 0}
                onClick={() => setAll(false)}>none</button>
        <span className="ctx-total">≈{fmt(loaded)} tokens loaded</span>
      </div>
      {held > 0 && (
        <p className="ctx-held">
          {held} change{held === 1 ? '' : 's'} to these files held for your approval.{' '}
          <Link to="/memory">Review</Link>
        </p>)}
      <ul className="ctx-list">
        {files.length === 0 && <EmptyState as="li">no files in this project yet</EmptyState>}
        {rows.map((f) => (
          <li key={f.path} className={f.selected || f.locked ? 'on' : ''}>
            <label title={f.locked ? 'Loaded into every prompt; not optional' : undefined}>
              <input type="checkbox" checked={f.selected || f.locked}
                     disabled={f.binary || busy || f.locked}
                     onChange={() => toggle(f.path)} />
              <span className="grow ellipsis">{f.path}</span>
            </label>
            {f.locked && <span className="ctx-flag">always</span>}
            {f.tainted && (
              <span className="ctx-flag warn"
                    title="Last written by a turn that had read outside content. Ticking it loads that text into every prompt.">
                written after outside content</span>)}
            <span className="ctx-tokens">{f.binary ? 'binary' : `≈${fmt(f.tokens)}`}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
