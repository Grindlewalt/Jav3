/* Settings: the things that are configured once and consulted everywhere. */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { Copy } from '../copy.jsx'
import { modelOption, setModel, useModel } from '../modelInfo.js'
import { notifyError } from '../notify.js'
import { useAuth } from '../auth.jsx'
import Page from '../components/Page.jsx'
import Card from '../components/Card.jsx'
import Button from '../components/Button.jsx'

const mmss = (secs) => {
  const s = Math.max(0, Math.round(secs))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function Settings() {
  const [msg, setMsg] = useState(null)
  const say = (m) => { setMsg(m); setTimeout(() => setMsg(null), 6000) }

  return (
    <Page title="Settings" className="settings-page">
      {msg && <p className="warn">{msg}</p>}
      <ModelPanel />
      <DevicesPanel say={say} />
      <MusicPanel say={say} />
      <SessionPanel />
    </Page>
  )
}

// --- model ------------------------------------------------------------------------

function ModelPanel() {
  const m = useModel()
  const active = modelOption(m, m?.active)
  return (
    <section className="panel">
      <h2>Model</h2>
      {!m ? <p className="dim">loading…</p> : (
        <>
          <div className="row">
            <select value={m.active}
                    onChange={(e) => setModel(e.target.value).catch(notifyError)}
                    disabled={m.choices.length < 2}>
              {m.choices.map((c) => (
                <option key={c} value={c}>{modelOption(m, c).label}</option>
              ))}
            </select>
            {m.active !== m.default && <span className="tag">override</span>}
          </div>
          <p className="dim small">
            <code>{active.id}</code>{active.blurb && <> — {active.blurb}</>}.
            Agents with a model pin of their own are unaffected.
          </p>
        </>
      )}
    </section>
  )
}

// --- devices (computers logged in with `jav3 login`) ------------------------------

// Add computer: this session mints a one-time code and the line the CLI wants.
// The session IS the authorization — whoever holds the line in the next few
// minutes gets a device token — so the line is shown with its countdown and
// cleared when it lapses or the operator is done with it.
function DevicesPanel({ say }) {
  const ask = useAsk()
  const [devices, setDevices] = useState(null)
  const [login, setLogin] = useState(null)       // {login, expires_at, ttl_seconds}
  const [left, setLeft] = useState(0)

  const load = useCallback(() => {
    api('/api/devices').then((r) => setDevices(r.devices)).catch(() => setDevices([]))
  }, [])
  useEffect(load, [load])

  useEffect(() => {
    if (!login) return undefined
    const tick = () => {
      const s = login.expires_at - Date.now() / 1000
      if (s <= 0) { setLogin(null); load() } else setLeft(s)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [login, load])

  async function addComputer() {
    try {
      setLogin(await api('/api/devices/login-code',
        { method: 'POST', body: JSON.stringify({ name: '' }) }))
    } catch (e) { say(e.detail || String(e)) }
  }

  async function revoke(d) {
    const ok = await ask.confirm(`Revoke access for “${d.name}”?`, {
      body: 'The CLI on that computer stops working immediately.',
      confirmLabel: 'Revoke', danger: true })
    if (!ok) return
    try { await api(`/api/devices/${d.id}`, { method: 'DELETE' }); load() }
    catch (e) { say(e.detail || String(e)) }
  }

  return (
    <section className="panel">
      <h2>Devices</h2>
      <p className="dim small">
        Log a computer’s <code>jav3</code> command-line client in to this server.
        Each computer gets its own revocable token. A token can chat with the agent
        using the same tools you have; it cannot reach secrets, the VM or other
        control panels.
      </p>
      {login ? (
        <div className="device-login">
          <p className="small">On the other computer run <code>jav3 login</code> and
            paste this line. It works once, for the next {mmss(left)}.</p>
          <div className="device-login-line">
            <code>{login.login}</code>
            <Copy text={login.login} />
          </div>
          <p className="dim small">No CLI there yet?{' '}
            <code>curl -fsSL {window.location.origin}/cli/install.sh | sh</code></p>
          <div className="row">
            <button className="ghost" onClick={() => { setLogin(null); load() }}>
              Done</button>
          </div>
        </div>
      ) : (
        <div className="row"><button onClick={addComputer}>Add computer</button></div>
      )}
      {devices === null ? <p className="dim">loading…</p>
        : devices.length === 0 ? <p className="dim small">No computers logged in.</p>
        : (
          <ul className="device-list">
            {devices.map((d) => (
              <li key={d.id} className="device-row">
                <span>
                  <strong>{d.name}</strong>
                  {d.hostname && <span className="dim"> · {d.hostname}</span>}
                  <span className="dim small">
                    {' · '}{d.last_seen ? `last seen ${d.last_seen} UTC` : 'never used'}</span>
                </span>
                <button className="ghost danger" onClick={() => revoke(d)}>Revoke</button>
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

// --- session ---------------------------------------------------------------------

// The door. Log out used to sit at the foot of the nav's overflow menu and
// again at the foot of the phone drawer — a way out on every screen for a
// thing done once a month. This card is the only one now.
function SessionPanel() {
  const { user, logout } = useAuth()
  return (
    <Card title="Session" headingLevel={2}>
      <p className="dim small">
        Signed in as <strong>{user?.username}</strong>. Logging out ends this
        browser’s session; computers logged in with <code>jav3</code> keep
        their own tokens until revoked above.
      </p>
      <div className="row">
        <Button variant="ghost" onClick={logout}>Log out</Button>
      </div>
    </Card>
  )
}

// --- music server --------------------------------------------------------------

function MusicPanel({ say }) {
  const [tm, setTm] = useState({ url: '' })
  const [test, setTest] = useState(null)
  useEffect(() => { api('/api/media/tarmac').then(setTm).catch(() => {}) }, [])

  async function save(e) {
    e.preventDefault()
    setTest(null)
    try {
      setTm(await api('/api/media/tarmac', {
        method: 'PUT', body: JSON.stringify({ url: tm.url }) }))
      say('Music server saved')
    } catch (err) { say(err.detail || String(err)) }
  }
  async function probe() {
    setTest({ testing: true })
    try {
      setTest(await api('/api/media/tarmac/test', { method: 'POST' }))
    } catch (err) { setTest({ ok: false, error: err.detail || String(err) }) }
  }
  return (
    <section className="panel">
      <h2>Music server</h2>
      <form className="row" onSubmit={save}>
        <input className="grow" placeholder="http://<host>:<port>"
               value={tm.url} onChange={(e) => setTm({ ...tm, url: e.target.value })} />
        <button type="submit">Save</button>
        <button type="button" className="ghost" onClick={probe} disabled={!tm.url}>
          Test</button>
      </form>
      {test && (
        <p className={test.ok ? 'badge' : 'error'}>
          {test.testing ? 'asking…' : test.ok
            ? `${test.status?.tracks ?? '?'} tracks · `
              + `${test.status?.players_connected ?? 0} player(s) open`
            : test.error}
        </p>
      )}
    </section>
  )
}
