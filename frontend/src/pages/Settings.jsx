/* Settings: the things that are configured once and consulted everywhere.
 *
 * Every card is the Card primitive and every control Input/Select/Button, so
 * one control is one size across the page (Model and Music used to be bare
 * <select>/<input> at the body's 15px beside Backup's 13px fields). */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { api, subscribeSse } from '../api.js'
import { dndBody, dndPresets, dndUntil } from '../dnd.js'
import { useAsk } from '../ask.jsx'
import { Copy } from '../copy.jsx'
import { ts } from '../format.js'
import { notifyError } from '../notify.js'
import { useAuth } from '../auth.jsx'
import { Button, Card, EmptyState, Input, Select, Tag, Toggle } from '../components/index.js'
import Page from '../components/Page.jsx'
import BackupPanel from '../BackupPanel.jsx'
import BrowserPanel from '../BrowserPanel.jsx'
import DeskPanel from '../DeskPanel.jsx'
import GroundingPanel from '../GroundingPanel.jsx'
import ProvidersPanel from '../ProvidersPanel.jsx'
import PermissionRulesPanel from '../PermissionRulesPanel.jsx'
import GiteaPanel from '../GiteaPanel.jsx'

const mmss = (secs) => {
  const s = Math.max(0, Math.round(secs))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function Settings() {
  return (
    <Page title="Settings" className="settings-page">
      <ProvidersPanel />
      <NotificationsPanel />
      <DevicesPanel />
      <DeskPanel />
      <GroundingPanel />
      <BrowserPanel />
      <PermissionRulesPanel />
      <GiteaPanel />
      <BackupPanel />
      <MusicPanel />
      <SessionPanel />
    </Page>
  )
}

// --- notifications ---------------------------------------------------------------
// What interrupts, and what the operator did themselves, are the server's
// calls (backend/security.py); the web toasts and the terminal client's sidebar
// both follow them. None of it hides anything: every event is still in
// Security's queue and history, and the badge still counts what waits.
const LEVEL_OPTIONS = [
  { value: 'critical', label: 'Critical only' },
  { value: 'approvals', label: 'Needs my approval' },
  { value: 'all', label: 'Everything' },
]
const MODES = [['ping', 'Ping'], ['badge', 'Badge'], ['record', 'Record']]

function NotificationsPanel() {
  const [s, setS] = useState(null)             // the server's view; false = could not load
  const { hash } = useLocation()
  useEffect(() => {
    api('/api/notifications/settings').then(setS).catch(() => setS(false))
  }, [])
  // do not disturb can be switched from the terminal, or end by itself
  useEffect(() => subscribeSse('/api/security/stream', (ev) => {
    if (ev.type !== 'dnd_changed') return
    setS((v) => (v ? { ...v, dnd: { on: ev.on, since: ev.since, until: ev.until,
                                    break_critical: ev.break_critical } } : v))
  }), [])
  useEffect(() => {
    if (hash === '#notifications' && s) document.getElementById('notifications')?.scrollIntoView()
  }, [hash, !!s]) // eslint-disable-line

  async function save(patch, optimistic) {
    const was = s
    if (optimistic) setS({ ...s, ...optimistic })
    try {
      setS(await api('/api/notifications/settings',
        { method: 'PUT', body: JSON.stringify(patch) }))
    } catch (e) { setS(was); notifyError(e) }
  }
  async function setDnd(choice) {
    const body = dndBody(choice)
    if (!body) return
    try {
      const d = await api('/api/notifications/dnd', { method: 'PUT', body: JSON.stringify(body) })
      setS((v) => ({ ...v, dnd: d }))
    } catch (e) { notifyError(e) }
  }

  return <NotificationsView s={s} save={save} setDnd={setDnd} />
}

// the card itself, from the server's view: exported so a test can render it
export function NotificationsView({ s, save, setDnd }) {
  if (s === null || s === false) {
    return (
      <Card title="Notifications" headingLevel={2} id="notifications">
        <p className="dim small settings-note">
          {s === null ? 'Loading…' : 'Could not load the notification settings.'}
        </p>
      </Card>
    )
  }
  const dnd = s.dnd || { on: false }
  const dndOptions = [
    { value: 'off', label: 'Off' },
    ...(dnd.on ? [{ value: 'current', label: `On ${dndUntil(dnd)}` }] : []),
    ...dndPresets(),
  ]
  return (
    <Card title="Notifications" headingLevel={2} id="notifications">
      <div className="settings-inline">
        <Select label="Ping me for" value={s.level}
                onChange={(e) => save({ level: e.target.value }, { level: e.target.value })}
                options={LEVEL_OPTIONS} />
        <span className="dim small notif-aside">critical always pings</span>
      </div>
      <div className="notif-row">
        <Toggle checked={s.self_quiet} label="Things I did myself: record only"
                onChange={(v) => save({ self_quiet: v }, { self_quiet: v })} />
        <span>Things I did myself: record only</span>
      </div>
      <p className="dim small settings-note">
        A profile edit, a LAN change, a package you approve, made by you in the web app or
        the terminal, is filed as “by you”, already acknowledged: no ping, not in the badge.
        The same change made by an agent still alerts. Critical alerts are never quieted.
      </p>

      <div className="settings-inline notif-dnd">
        <Select label="Do not disturb" value={dnd.on ? 'current' : 'off'}
                onChange={(e) => e.target.value !== 'current' && setDnd(e.target.value)}
                options={dndOptions} />
        <div className="notif-row">
          <Toggle checked={s.dnd_break_critical} label="Critical alerts break through do not disturb"
                  onChange={(v) => save({ dnd_break_critical: v }, { dnd_break_critical: v })} />
          <span>critical breaks through</span>
        </div>
      </div>
      <p className="dim small settings-note">
        While it is on nothing pings and the top bar says DND. Alerts still count in Security
        and everything is still recorded; approvals a chat is waiting on stay where they
        are. When it ends you get one summary.
      </p>

      <div className="notif-kinds" role="group" aria-label="What each kind of alert does">
        <table>
          <thead>
            <tr>
              <th scope="col">Per kind</th>
              {MODES.map(([m, label]) => <th key={m} scope="col" className="notif-mode">{label}</th>)}
              <th scope="col"><span className="sr-only">reset</span></th>
            </tr>
          </thead>
          <tbody>
            {s.kinds.map((k) => (
              <tr key={k.kind} className={k.chosen ? 'chosen' : ''}>
                <th scope="row" className="mono small" title={`usually ${k.usual}`}>{k.kind}</th>
                {k.locked ? (
                  <td colSpan={3} className="dim small">locked: always pings</td>
                ) : MODES.map(([m, label]) => (
                  <td key={m} className="notif-mode">
                    <input type="radio" name={`mode-${k.kind}`} checked={k.mode === m}
                           aria-label={`${k.kind}: ${label}`} title={m === k.default ? 'default' : ''}
                           onChange={() => save({ kinds: { [k.kind]: m } })} />
                  </td>))}
                <td className="notif-reset">
                  {k.chosen && !k.locked && (
                    <button type="button" className="ghost small" title={`back to the default (${k.default})`}
                            onClick={() => save({ kinds: { [k.kind]: null } })}>reset</button>)}
                </td>
              </tr>))}
          </tbody>
        </table>
      </div>
      <p className="dim small settings-note">
        Ping: a toast, and the terminal's sidebar. Badge: counts in Security until you
        acknowledge it, never pings. Record: filed already acknowledged, kept in the history.
        A kind you have not set follows “Ping me for”.
      </p>
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
        Log a computer’s <code>jav3</code> command-line client, or its
        {' '}<code>jav3-desk</code> computer-use client, in to this server. Each
        gets its own revocable token. A <code>jav3</code> token can chat with the
        agent using the same tools you have; a <code>jav3-desk</code> token can
        only connect for computer use. Neither reaches secrets, the VM or other
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
                    {d.scope === 'desk' && <Tag>computer use</Tag>}
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
