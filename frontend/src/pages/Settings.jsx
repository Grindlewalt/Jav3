/* Settings: the things that are configured once and consulted everywhere. */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { Block } from '../copy.jsx'
import { modelOption, setModel, useModel } from '../modelInfo.js'
import { notifyError } from '../notify.js'

export default function Settings() {
  const [msg, setMsg] = useState(null)
  const say = (m) => { setMsg(m); setTimeout(() => setMsg(null), 6000) }

  return (
    <div className="page settings-page">
      <h1>Settings</h1>
      {msg && <p className="warn">{msg}</p>}
      <ModelPanel />
      <DevicesPanel say={say} />
      <MusicPanel say={say} />
    </div>
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

// --- devices (API tokens for a CLI/machine via the pairing flow) -----------------

// The command the operator runs on the device: claim the code, wait for the
// browser confirm, then save the minted token 0600. curl + sed so it needs no
// jq. String.raw keeps the sed backslashes intact; only ${origin}/${code}
// interpolate (both from safe sources — the app origin and the code alphabet).
function claimScript(origin, code) {
  return String.raw`BASE="${origin}"
CODE="${code}"
name="$(hostname 2>/dev/null || echo cli)"
plat="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
secret="$(curl -fsS -X POST "$BASE/api/devices/pair/claim" -H 'content-type: application/json' -d "{\"code\":\"$CODE\",\"name\":\"$name\",\"hostname\":\"$name\",\"platform\":\"$plat\"}" | sed -n 's/.*"device_secret":"\([^"]*\)".*/\1/p')"
echo "Approve this device at: $BASE/pair/$CODE"
while :; do
  r="$(curl -fsS -X POST "$BASE/api/devices/pair/poll" -H 'content-type: application/json' -d "{\"code\":\"$CODE\",\"device_secret\":\"$secret\"}")"
  case "$r" in *'"denied"'*) echo "Denied by the operator."; exit 1;; esac
  token="$(printf '%s' "$r" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  [ -n "$token" ] && break
  sleep 3
done
mkdir -p "$HOME/.config/jarvis"
(umask 177; printf '%s\n' "$token" > "$HOME/.config/jarvis/device-token")
chmod 600 "$HOME/.config/jarvis/device-token" 2>/dev/null || true
echo "Paired. Token saved to ~/.config/jarvis/device-token (send it as: Authorization: Bearer <token>)"`
}

function DevicesPanel({ say }) {
  const ask = useAsk()
  const [devices, setDevices] = useState(null)
  const [ticket, setTicket] = useState(null)
  const origin = window.location.origin

  const load = useCallback(() => {
    api('/api/devices').then((r) => setDevices(r.devices)).catch(() => setDevices([]))
  }, [])
  useEffect(load, [load])

  async function enroll() {
    const name = await ask.prompt('Name this device', '',
      { placeholder: 'e.g. laptop-cli' })
    if (name === null) return
    try {
      setTicket(await api('/api/devices/enroll',
        { method: 'POST', body: JSON.stringify({ name: name || '' }) }))
    } catch (e) { say(e.detail || String(e)) }
  }

  async function revoke(d) {
    const ok = await ask.confirm(`Revoke access for “${d.name}”?`, {
      body: 'Any CLI using this token stops working immediately.',
      confirmLabel: 'Revoke', danger: true })
    if (!ok) return
    try { await api(`/api/devices/${d.id}`, { method: 'DELETE' }); load() }
    catch (e) { say(e.detail || String(e)) }
  }

  const script = ticket ? claimScript(origin, ticket.code) : ''

  return (
    <section className="panel">
      <h2>Devices</h2>
      <p className="dim small">
        Authorize a CLI or machine to reach Jarvis’s API without pasting a key in
        a terminal. Enroll here, run the command on the device, then confirm it
        in the browser — it receives a revocable token, shown once and never
        again. A token can drive Jarvis’s agent (chat) with the same tools you
        have; it cannot reach the secrets or VM control panels.
        Revoke it any time below.
      </p>
      {ticket ? (
        <div className="device-enroll">
          <p>Code <code>{ticket.code}</code> — run this on the device, then{' '}
            <a href={`/pair/${ticket.code}`} target="_blank" rel="noreferrer">
              approve it</a>. Good for about 15&nbsp;minutes.</p>
          <Block text={script} />
          <div className="row">
            <button className="ghost" onClick={() => { setTicket(null); load() }}>
              Done</button>
          </div>
        </div>
      ) : (
        <div className="row"><button onClick={enroll}>Enroll a device</button></div>
      )}
      {devices === null ? <p className="dim">loading…</p>
        : devices.length === 0 ? <p className="dim small">No devices enrolled.</p>
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
