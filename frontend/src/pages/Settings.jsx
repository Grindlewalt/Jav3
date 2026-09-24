/* Settings: the things that are configured once and consulted everywhere.
 *
 * Every card is the Card primitive and every control Input/Select/Button, so
 * one control is one size across the page (Model and Music used to be bare
 * <select>/<input> at the body's 15px beside Backup's 13px fields). */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { Copy } from '../copy.jsx'
import { ts } from '../format.js'
import { modelOption, setModel, useModel } from '../modelInfo.js'
import { notifyError } from '../notify.js'
import { useAuth } from '../auth.jsx'
import { Button, Card, EmptyState, Input, Select, Tag } from '../components/index.js'
import Page from '../components/Page.jsx'
import BackupPanel from '../BackupPanel.jsx'

const mmss = (secs) => {
  const s = Math.max(0, Math.round(secs))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function Settings() {
  return (
    <Page title="Settings" className="settings-page">
      <ModelPanel />
      <DevicesPanel />
      <BackupPanel />
      <MusicPanel />
      <SessionPanel />
    </Page>
  )
}

// --- model ------------------------------------------------------------------------

function ModelPanel() {
  const m = useModel()
  const active = modelOption(m, m?.active)
  return (
    <Card title="Model" headingLevel={2}
          actions={m && m.active !== m.default ? <Tag>override</Tag> : null}>
      {!m ? <p className="dim small">loading…</p> : (
        <div className="stack">
          <Select aria-label="Model" value={m.active}
                  onChange={(e) => setModel(e.target.value).catch(notifyError)}
                  disabled={m.choices.length < 2}
                  options={m.choices.map((c) => ({ value: c, label: modelOption(m, c).label }))} />
          <p className="dim small settings-note">
            <code>{active.id}</code>{active.blurb && <> — {active.blurb}</>}.
            Agents with a model pin of their own are unaffected.
          </p>
        </div>
      )}
    </Card>
  )
}

// --- devices (computers logged in with `jav3 login`) ------------------------------

// Add computer: this session mints a one-time code and the line the CLI wants.
// The session IS the authorization — whoever holds the line in the next few
// minutes gets a device token — so the line is shown with its countdown, and
// dismissing it (Done, or leaving the page) cancels the code on the server
// too rather than leaving it live for the rest of its TTL.
const cancelLoginCode = () =>
  api('/api/devices/login-code', { method: 'DELETE' }).catch(() => {})

function DevicesPanel() {
  const ask = useAsk()
  const [devices, setDevices] = useState(null)
  const [login, setLogin] = useState(null)       // {login, install, ttl_seconds, plain_http, deadline}
  const [left, setLeft] = useState(0)
  const live = useRef(false)

  const load = useCallback(() => {
    api('/api/devices').then((r) => setDevices(r.devices)).catch(() => setDevices([]))
  }, [])
  useEffect(load, [load])

  // leaving Settings with a code on screen cancels it
  useEffect(() => () => { if (live.current) cancelLoginCode() }, [])

  useEffect(() => {
    if (!login) return undefined
    // counted from the server's ttl_seconds on this page's monotonic clock,
    // never from the browser's wall clock against a server timestamp
    const tick = () => {
      const s = login.deadline - performance.now() / 1000
      if (s <= 0) { live.current = false; setLogin(null); load() } else setLeft(s)
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [login, load])

  async function addComputer() {
    try {
      const r = await api('/api/devices/login-code',
        { method: 'POST', body: JSON.stringify({ name: '' }) })
      live.current = true
      setLogin({ ...r, deadline: performance.now() / 1000 + r.ttl_seconds })
    } catch (e) { notifyError(e) }
  }

  function done() {
    live.current = false
    setLogin(null)
    cancelLoginCode().then(load)
  }

  async function revoke(d) {
    const ok = await ask.confirm(`Revoke access for “${d.name}”?`, {
      body: 'The CLI on that computer stops working immediately.',
      confirmLabel: 'Revoke', danger: true })
    if (!ok) return
    try { await api(`/api/devices/${d.id}`, { method: 'DELETE' }); load() }
    catch (e) { notifyError(e) }
  }

  // the line is `address=… code=…`; each half is its own unbreakable-looking
  // piece so a phone wraps BETWEEN them, and a code too long for the line
  // breaks at a character rather than at the hyphen inside it (which read as
  // a line-break hyphen someone might type)
  const parts = login ? login.login.split(' ') : []

  return (
    <Card title="Devices" headingLevel={2}>
      <p className="dim small settings-note">
        Log a computer’s <code>jav3</code> command-line client in to this server.
        Each computer gets its own revocable token. A token can chat with the agent
        using the same tools you have; it cannot reach secrets, the VM or other
        control panels.
      </p>
      {login ? (
        <div className="device-login">
          <p className="small settings-note">On the other computer run
            {' '}<code>jav3 login</code> and paste this line. It works once, for
            the next {mmss(left)}.</p>
          <div className="device-login-line">
            <code>
              {parts.map((p, i) => (
                <span key={i}>{i > 0 && ' '}<span className="device-login-part">{p}</span></span>
              ))}
            </code>
            <Copy text={login.login} />
          </div>
          {login.plain_http && (
            <p className="warn settings-note">This address is plain http: the code,
              and the token it is traded for, cross the network unencrypted. Only
              use it on a network you trust.</p>)}
          {login.install && (
            <p className="dim small settings-note">No CLI there yet?{' '}
              <code className="device-install">{login.install}</code></p>)}
          <div className="settings-actions">
            <Button variant="ghost" onClick={done}>Done</Button>
          </div>
        </div>
      ) : (
        <div className="settings-actions">
          <Button onClick={addComputer}>Add computer</Button>
        </div>
      )}
      {devices === null ? <EmptyState>loading…</EmptyState>
        : devices.length === 0 ? <EmptyState>No computers logged in.</EmptyState>
        : (
          <ul className="device-list">
            {devices.map((d) => (
              <li key={d.id} className="device-row">
                <div className="device-main">
                  <div className="device-name">
                    <strong className="ellipsis" title={d.name}>{d.name}</strong>
                    {/* the CLI names a computer after its hostname by default,
                        so the two are usually the same word printed twice */}
                    {d.hostname && d.hostname !== d.name && (
                      <span className="dim small ellipsis" title={d.hostname}>{d.hostname}</span>)}
                  </div>
                  <div className="dim small device-meta">
                    <span>{d.last_used_at ? `Last used ${ts(d.last_used_at)} UTC`
                      : 'Never used'}</span>
                    <span>{d.idle_expires_at < d.expires_at
                      ? `Expires ${ts(d.idle_expires_at)} UTC if unused`
                      : `Expires ${ts(d.expires_at)} UTC`}</span>
                  </div>
                </div>
                <Button variant="ghost" danger onClick={() => revoke(d)}>Revoke</Button>
              </li>
            ))}
          </ul>
        )}
    </Card>
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
      <p className="dim small settings-note">
        Signed in as <strong>{user?.username}</strong>. Logging out ends this
        browser’s session; computers logged in with <code>jav3</code> keep
        their own tokens until revoked above.
      </p>
      <div className="settings-actions">
        <Button variant="ghost" onClick={logout}>Log out</Button>
      </div>
    </Card>
  )
}

// --- music server --------------------------------------------------------------

function MusicPanel() {
  const [tm, setTm] = useState({ url: '' })
  const [saved, setSaved] = useState('')     // the url as the server holds it
  const [test, setTest] = useState(null)
  useEffect(() => {
    api('/api/media/tarmac').then((r) => { setTm(r); setSaved(r.url || '') })
      .catch(() => {})
  }, [])

  async function save(e) {
    e.preventDefault()
    setTest(null)
    try {
      const r = await api('/api/media/tarmac', {
        method: 'PUT', body: JSON.stringify({ url: tm.url }) })
      setTm(r); setSaved(r.url || '')
    } catch (err) { notifyError(err) }
  }
  async function probe() {
    setTest({ testing: true })
    try {
      setTest(await api('/api/media/tarmac/test', { method: 'POST' }))
    } catch (err) { setTest({ ok: false, error: err.detail || String(err) }) }
  }
  const dirty = (tm.url || '') !== saved
  return (
    <Card title="Music server" headingLevel={2}>
      {/* the field takes the line; Save and Test travel together, so on a
          phone they drop under it as a pair instead of Test wrapping alone */}
      <form className="settings-inline" onSubmit={save}>
        <Input aria-label="Music server address" placeholder="http://<host>:<port>"
               value={tm.url || ''} onChange={(e) => setTm({ ...tm, url: e.target.value })} />
        <div className="settings-inline-actions">
          <Button type="submit" disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</Button>
          <Button variant="ghost" onClick={probe} disabled={!saved || test?.testing}>
            Test</Button>
        </div>
      </form>
      {test && (
        <p className={`small settings-note ${test.ok ? 'dim' : 'error'}`}>
          {test.testing ? 'Asking…' : test.ok
            ? `${test.status?.tracks ?? '?'} tracks · `
              + `${test.status?.players_connected ?? 0} player(s) open`
            : test.error}
        </p>
      )}
    </Card>
  )
}
