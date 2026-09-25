import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// Pick which project files are loaded into Jav3's context. Nothing is
// loaded by default — tick a file to include its full contents; the token
// count and running total keep you honest about how big the context gets.
export default function ContextPanel({ slug }) {
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
          files: on ? files.filter((f) => !f.binary).map((f) => f.path) : [] }) })
      await refresh()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      <div className="row">
        <span className="grow dim">nothing loads by default — tick to include</span>
        <button className="ghost" disabled={busy || files.length === 0}
                onClick={() => setAll(true)}>all</button>
        <button className="ghost" disabled={busy || files.length === 0}
                onClick={() => setAll(false)}>none</button>
        <span className="ctx-total">≈{fmt(total)} tokens loaded</span>
      </div>
      <ul className="ctx-list">
        {files.length === 0 && <EmptyState as="li">no files in this project yet</EmptyState>}
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
