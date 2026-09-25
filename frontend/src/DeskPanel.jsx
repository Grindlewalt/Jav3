/* Settings → Computer use: one row per computer logged in with `jav3-desk`
 * (backend/desk.py, desk_api.py).
 *
 * The grants are the server's half of the gate: Screen, Input, Shell, set
 * here and nowhere else. The computer's own ceiling is the other half — shell
 * reads "off until enabled at the computer" while `jav3-desk allow-shell` has
 * not been run there, whatever this switch says. Stop turns every grant off
 * and drops the session. */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ago } from './format.js'
import { notifyError } from './notify.js'
import Button from './components/Button.jsx'
import Card from './components/Card.jsx'
import EmptyState from './components/EmptyState.jsx'
import Input from './components/Input.jsx'
import Select from './components/Select.jsx'
import Tag from './components/Tag.jsx'
import Toggle from './components/Toggle.jsx'

const POLL_MS = 3000

export default function DeskPanel() {
  const [data, setData] = useState(null)       // {desks, trusted_minutes}

  const load = useCallback(() => api('/api/desk').then(setData)
    .catch(() => setData((d) => d || { desks: [], pending: [] })), [])

  useEffect(() => {
    load()
    const id = setInterval(() => { if (!document.hidden) load() }, POLL_MS)
    return () => clearInterval(id)
  }, [load])

  const put = async (id, body) => {
    try {
      await api(`/api/desk/${id}/grants`, { method: 'PUT', body: JSON.stringify(body) })
    } catch (e) { notifyError(e) }
    load()
  }

  const stop = async (id) => {
    try { await api(`/api/desk/${id}/stop`, { method: 'POST' }) }
    catch (e) { notifyError(e) }
    load()
  }

  return (
    <Card title="Computer use" headingLevel={2} id="desk">
      {data === null ? <EmptyState>loading…</EmptyState>
        : data.desks.length === 0 ? (
          <EmptyState>No computers. Log one in with <code>jav3-desk login</code>.</EmptyState>
        ) : (
          <ul className="device-list desk-list">
            {data.desks.map((d) => (
              <DeskRow key={d.id} d={d} minutes={data.trusted_minutes}
                       put={(b) => put(d.id, b)} stop={() => stop(d.id)} />
            ))}
          </ul>
        )}
    </Card>
  )
}

function DeskRow({ d, minutes, put, stop }) {
  const g = d.grants
  const [pattern, setPattern] = useState('')
  const localShell = d.ceiling ? d.ceiling.shell : null      // null = offline, unknown
  const addPattern = (e) => {
    e.preventDefault()
    const p = pattern.trim()
    if (!p) return
    put({ allowlist: [...g.allowlist, p] })
    setPattern('')
  }
  return (
    <li className="desk-row">
      <div className="desk-head">
        <span className={`desk-dot ${d.online ? 'on' : ''}`} role="img"
              aria-label={d.online ? 'online' : 'offline'} />
        <strong className="ellipsis" title={d.name}>{d.name}</strong>
        {d.backend && <Tag>{d.backend}</Tag>}
        <span className="dim small desk-last">
          {d.last_action_at ? ago(d.last_action_at) : 'no actions'}
        </span>
        <Button variant="ghost" danger onClick={stop}
                disabled={!d.online && !g.screen && !g.input && g.shell === 'off'}>
          Stop</Button>
      </div>
      <div className="desk-grants">
        <Toggle label={`Screen on ${d.name}`} onText="Screen" offText="Screen"
                checked={g.screen} onChange={(v) => put({ screen: v })} />
        <Toggle label={`Input on ${d.name}`} onText="Input" offText="Input"
                checked={g.input} onChange={(v) => put({ input: v })} />
        <Toggle label={`Shell on ${d.name}`} onText="Shell" offText="Shell"
                checked={g.shell !== 'off'}
                onChange={(v) => put({ shell: v ? 'ask' : 'off' })} />
        {g.shell !== 'off' && (
          <Select aria-label={`Shell mode on ${d.name}`} value={g.shell}
                  onChange={(e) => put({ shell: e.target.value })}
                  options={[{ value: 'ask', label: 'Ask' },
                            { value: 'trusted', label: `Trusted · ${minutes} min` }]} />
        )}
        {g.shell !== 'off' && localShell === false && (
          <span className="warn small">off until enabled at the computer</span>
        )}
      </div>
      {g.shell !== 'off' && (
        <div className="desk-allow">
          {g.allowlist.map((p) => (
            <Tag key={p} className="desk-pattern">
              <code>{p}</code>
              <button type="button" className="desk-pattern-x" aria-label={`Remove ${p}`}
                      onClick={() => put({ allowlist: g.allowlist.filter((x) => x !== p) })}>
                ×</button>
            </Tag>
          ))}
          <form className="settings-inline desk-allow-add" onSubmit={addPattern}>
            <Input aria-label={`Allowlist pattern for ${d.name}`} placeholder="git status"
                   value={pattern} onChange={(e) => setPattern(e.target.value)} />
            <Button type="submit" variant="ghost" disabled={!pattern.trim()}>Allow</Button>
          </form>
        </div>
      )}
    </li>
  )
}
