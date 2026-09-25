import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { notify, notifyError } from '../notify.js'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

export default function OrganizerPanel({ slug }) {
  const [dirs, setDirs] = useState([])
  const [files, setFiles] = useState([])
  const [over, setOver] = useState(null)
  const uploadRef = useRef(null)
  const uploadDest = useRef('')
  const ask = useAsk()

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
    } catch (err) { notifyError(err) }
    refresh()
  }

  async function newDir() {
    const path = await ask.prompt('New directory', '',
      { placeholder: 'e.g. images, docs/refs', confirmLabel: 'Next' })
    if (!path) return
    const mark = await ask.prompt('Mark for Jav3 — what belongs here?', '',
      { body: 'Optional.', confirmLabel: 'Create directory' }) || ''
    await api(`/api/projects/${slug}/mkdir`, {
      method: 'POST', body: JSON.stringify({ path, mark }) })
    refresh()
  }

  async function editMark(dir) {
    const mark = await ask.prompt(
      `Mark for ${dir.path || 'project root'}`, dir.mark,
      { body: 'Tell Jav3 what goes here.', confirmLabel: 'Save' })
    if (mark === null) return
    await api(`/api/projects/${slug}/dirs/mark`, {
      method: 'PUT', body: JSON.stringify({ path: dir.path, mark }) })
    refresh()
  }

  async function rmDir(dir) {
    try {
      await api(`/api/projects/${slug}/dirs?path=${encodeURIComponent(dir)}`,
                { method: 'DELETE' })
    } catch (err) { notifyError(err) }
    refresh()
  }

  async function del(path) {
    if (!await ask.confirm(`Delete ${path}?`,
                           { confirmLabel: 'Delete', danger: true })) return
    await api(`/api/projects/${slug}/file?path=${encodeURIComponent(path)}`,
              { method: 'DELETE' })
    refresh()
  }

  async function onUpload(e) {
    const file = e.target.files?.[0]
    if (!file) return
    const form = new FormData()
    form.append('file', file)
    const res = await fetch(`/api/projects/${slug}/upload?dest=${encodeURIComponent(uploadDest.current)}`,
                            { method: 'POST', body: form })
    if (!res.ok) {
      const detail = await res.json().then((d) => d.detail).catch(() => res.statusText)
      notify(`upload failed: ${detail}`)
    }
    e.target.value = ''
    refresh()
  }

  return (
    <div className="pane-col">
      <div className="row">
        <span className="dim grow">drag files between directories · marks tell Jav3
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
              <span className="dir-name">{d.path ? `${d.path}/` : 'project root'}</span>
              <button type="button" className={d.mark ? 'dir-mark' : 'dir-mark unset'}
                      onClick={() => editMark(d)} title="edit the mark Jav3 reads">
                {d.mark || 'add a mark'}
              </button>
              <button className="win-btn" title="upload here"
                      onClick={() => { uploadDest.current = d.path; uploadRef.current.click() }}>⤒</button>
              {d.path && inDir(d.path).length === 0 &&
                <button className="win-btn" title="remove empty dir"
                        onClick={() => rmDir(d.path)}>×</button>}
            </div>
            {inDir(d.path).map((p) => (
              <div key={p} className="file-row" draggable
                   onDragStart={(e) => e.dataTransfer.setData('text/plain', p)}>
                <span className="grow ellipsis">{p.split('/').pop()}</span>
                <button className="win-btn" title="delete" aria-label={`delete ${p}`}
                        onClick={() => del(p)}>×</button>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
