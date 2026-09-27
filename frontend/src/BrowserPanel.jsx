/* Settings → Browser use: one row per browser paired with the jav3-browser
 * extension (backend/browser.py, browser_api.py).
 *
 * Grants are per browser AND per project, set here and nowhere else: Read
 * (open, navigate, read, scroll, screenshot, list, close) and Act (click,
 * type; implies Read). "Every project" applies wherever a project has no row
 * of its own. The extension's own controls (per-site consent, Cancel, Pause)
 * sit on top and cannot be overridden from here. Stop removes every grant and
 * drops the socket. */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ago } from './format.js'
import { notifyError } from './notify.js'
import Button from './components/Button.jsx'
import Card from './components/Card.jsx'
import EmptyState from './components/EmptyState.jsx'
import Select from './components/Select.jsx'
import Tag from './components/Tag.jsx'
import Toggle from './components/Toggle.jsx'

const POLL_MS = 5000
const label = (p) => (p === '*' ? 'Every project' : p === '' ? 'Chats with no project' : p)

export default function BrowserPanel() {
  const [data, setData] = useState(null)
  const [projects, setProjects] = useState([])

  const load = useCallback(() => api('/api/browser').then(setData)
    .catch(() => setData((d) => d || { browsers: [] })), [])

  useEffect(() => {
    load()
    api('/api/projects').then((r) => setProjects((r.projects || []).map((p) => p.slug)))
      .catch(() => {})
    const id = setInterval(() => { if (!document.hidden) load() }, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const put = async (id, body) => {
    try {
      await api(`/api/browser/${id}/grants`, { method: 'PUT', body: JSON.stringify(body) })
    } catch (e) { notifyError(e) }
    load()
  }

  const stop = async (id) => {
    try { await api(`/api/browser/${id}/stop`, { method: 'POST' }) }
    catch (e) { notifyError(e) }
    load()
  }

  return (
    <Card title="Browser use" headingLevel={2} id="browser">
      {data === null ? <EmptyState>loading…</EmptyState>
        : data.browsers.length === 0 ? (
          <EmptyState>No browsers. Install the extension from{' '}
            <a href="/cli/jav3-browser.zip">jav3-browser.zip</a> and pair it with the
            Add computer line.</EmptyState>
        ) : (
          <ul className="device-list desk-list">
            {data.browsers.map((b) => (
              <BrowserRow key={b.id} b={b} projects={projects}
                          put={(body) => put(b.id, body)} stop={() => stop(b.id)} />
            ))}
          </ul>
        )}
    </Card>
  )
}

function BrowserRow({ b, projects, put, stop }) {
  const [adding, setAdding] = useState('*')
  const have = new Set(b.grants.map((g) => g.project))
  const choices = ['*', '', ...projects].filter((p) => !have.has(p))
  return (
    <li className="desk-row">
      <div className="desk-head">
        <span className={`desk-dot ${b.online ? 'on' : ''}`} role="img"
              aria-label={b.online ? 'online' : 'offline'} />
        <strong className="ellipsis" title={b.name}>{b.name}</strong>
        {b.paused && <Tag>paused in the browser</Tag>}
        <span className="dim small desk-last">
          {b.last_action_at ? ago(b.last_action_at) : 'no actions'}
        </span>
        <Button variant="ghost" danger onClick={stop}
                disabled={!b.online && b.grants.length === 0}>Stop</Button>
      </div>
      {b.grants.map((g) => (
        <div className="desk-grants" key={g.project}>
          <span className="small">{label(g.project)}</span>
          <Toggle label={`Read for ${label(g.project)} on ${b.name}`} onText="Read"
                  offText="Read" checked={g.read}
                  onChange={(v) => put({ project: g.project, read: v, act: v && g.act })} />
          <Toggle label={`Act for ${label(g.project)} on ${b.name}`} onText="Act"
                  offText="Act" checked={g.act}
                  onChange={(v) => put({ project: g.project, read: g.read || v, act: v })} />
        </div>
      ))}
      {choices.length > 0 && (
        <div className="desk-grants">
          <Select aria-label={`Project to grant ${b.name} to`} value={adding}
                  onChange={(e) => setAdding(e.target.value)}
                  options={choices.map((p) => ({ value: p, label: label(p) }))} />
          <Button variant="ghost" disabled={!choices.includes(adding)}
                  onClick={() => put({ project: adding, read: true, act: false })}>
            Grant Read</Button>
        </div>
      )}
    </li>
  )
}
