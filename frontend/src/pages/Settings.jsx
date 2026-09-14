/* Settings: the things that are configured once and consulted everywhere.
 *
 * These panels lived on the Computer use tab because that is where they were
 * first needed, and the tab became a place you scrolled past three credential
 * forms to reach the machine you came for. They are here now, and the tab is
 * about machines again.
 *
 * At the bottom, in red, the things that should be hard to do by accident:
 * making the Cloudflare Access secret readable, and rotating the pairing token.
 * Each takes a typed word and a held button. The ceremony is the control —
 * the server checks the word too, so a request that skips the page does not
 * skip it — and both are on the record in the Review Center.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { Block } from '../copy.jsx'
import { useDismiss } from '../useDismiss.js'

// Cloudflare's dashboard shows a service token as whole header lines, so that is
// what gets pasted. Strip the header name, quotes and whitespace rather than
// letting "CF-Access-Client-Id: abc.access" through as the id.
function cleanToken(v) {
  return String(v || '')
    .replace(/^\s*CF[-_]?Access[-_]?Client[-_]?(Id|Secret)\s*[:=]\s*/i, '')
    .replace(/^["'`]|["'`]$/g, '')
    .replace(/\s+/g, '')
    .trim()
}

const mmss = (secs) => {
  const s = Math.max(0, Math.round(secs))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

export default function Settings() {
  const [msg, setMsg] = useState(null)
  const say = (m) => { setMsg(m); setTimeout(() => setMsg(null), 6000) }

  return (
    <div className="page settings-page">
      <h1>Settings</h1>
      {msg && <p className="warn">{msg}</p>}
      <ModelPanel />
      <DevicesPanel say={say} />
      <AccessPanel />
      <MusicPanel say={say} />
      <JellyfinPanel say={say} />
      <DangerZone say={say} />
    </div>
  )
}

// --- model ------------------------------------------------------------------------

function ModelPanel() {
  const [m, setM] = useState(null)
  useEffect(() => {
    api('/api/model').then(setM).catch(() => {})
    const h = (e) => setM(e.detail)
    window.addEventListener('jarvis-model-changed', h)
    return () => window.removeEventListener('jarvis-model-changed', h)
  }, [])
  async function pick(model) {
    const next = await api('/api/model', { method: 'PUT', body: JSON.stringify({ model }) })
    setM(next)
    window.dispatchEvent(new CustomEvent('jarvis-model-changed', { detail: next }))
  }
  return (
    <section className="panel">
      <h2>Model</h2>
      {!m ? <p className="dim">loading…</p> : (
        <>
          <div className="row">
            <select value={m.active} onChange={(e) => pick(e.target.value)}
                    disabled={m.choices.length < 2}>
              {m.choices.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
            {m.active !== m.default && <span className="tag">override</span>}
          </div>
          <p className="dim small">
            <code>deepseek-flash</code> is DeepSeek's name for the current
            Flash — V4.1 Flash since 10 Sep 2026 — so it follows their
            releases without a change here. Pro is not offered: the API routes
            it to Flash at Flash price until a V4.1 Pro exists. Agents with a
            model pin of their own are unaffected.
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
        have; it cannot reach the secrets, computer-use, or VM control panels.
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

// --- Cloudflare Access ------------------------------------------------------------

function AccessPanel() {
  const [cf, setCf] = useState({ configured: false, client_id: '', hosts: [] })
  const [cfId, setCfId] = useState('')
  const [cfSecret, setCfSecret] = useState('')
  const [result, setResult] = useState(null)
  const load = () => api('/api/computeruse/cfaccess')
    .then((r) => { setCf(r); setCfId(r.client_id || '') }).catch(() => {})
  useEffect(() => { load() }, [])

  // Rotating is the whole reason this panel exists, so it reports which
  // machines took the new token and which it could not reach — the second list
  // is the operator's remaining work, and leaving it out would imply the
  // rotation was complete when it was not.
  async function save(e) {
    e.preventDefault()
    setResult(null)
    try {
      const r = await api('/api/computeruse/cfaccess', {
        method: 'PUT', body: JSON.stringify({ client_id: cfId, secret: cfSecret }) })
      setCfSecret('')
      setResult(r)
      load()
    } catch (err) { setResult({ error: err.detail || String(err) }) }
  }

  return (
    <section className="panel">
      <h2>Cloudflare Access token</h2>
      <form className="row" onSubmit={save}>
        <input className="grow" placeholder="Client Id (ends in .access)"
               value={cfId} onChange={(e) => setCfId(cleanToken(e.target.value))} />
        <input type="password"
               placeholder={cf.configured ? 'secret (stored)' : 'Client Secret'}
               value={cfSecret}
               onChange={(e) => setCfSecret(cleanToken(e.target.value))} />
        <button type="submit" disabled={!cfId || !cfSecret}>Save & push</button>
      </form>
      {result && (result.error
        ? <p className="error">{result.error}</p>
        : <p className="badge">
            Saved.{' '}
            {result.updated?.length
              ? `Pushed to ${result.updated.join(', ')} — each takes it on its next reconnect.`
              : 'No machine was connected to push it to.'}
            {result.missed?.length ? ` Could not reach ${result.missed.join(', ')}.` : ''}
          </p>)}
      <p className="dim small">
        One token, held here and used for everything: Jarvis, the music server,
        and every paired machine. A new machine receives it by pairing, so it is
        never typed or pasted; saving a rotated one pushes it to every machine
        that is connected right now. A machine that is offline cannot be told
        — Jarvis is behind the thing being rotated — so that one is paired
        again. The secret is not readable from here; see the bottom of this
        page for the testing-only exception.
      </p>
    </section>
  )
}

// --- music server / Jellyfin ----------------------------------------------------

function MusicPanel({ say }) {
  const [tm, setTm] = useState({ url: '', cf_id: '', secret_set: false })
  const [test, setTest] = useState(null)
  useEffect(() => { api('/api/computeruse/tarmac').then(setTm).catch(() => {}) }, [])

  // Only the URL. The music server's Access token stopped being its own thing
  // — it is the one token above, and having a second copy here is precisely
  // how rotating it broke music while everything else looked fine.
  async function save(e) {
    e.preventDefault()
    setTest(null)
    try {
      setTm(await api('/api/computeruse/tarmac', {
        method: 'PUT', body: JSON.stringify({ url: tm.url }) }))
      say('Music server saved')
    } catch (err) { say(err.detail || String(err)) }
  }
  async function probe() {
    setTest({ testing: true })
    try {
      setTest(await api('/api/computeruse/tarmac/test', { method: 'POST' }))
    } catch (err) { setTest({ ok: false, error: err.detail || String(err) }) }
  }
  return (
    <section className="panel">
      <h2>Music server</h2>
      <form className="row" onSubmit={save}>
        <input className="grow" placeholder="https://music.atomos.network"
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
      <p className="dim small">
        It is a separate Cloudflare Access application, so the token above
        needs its own Service Auth policy there as well as on this one.
      </p>
    </section>
  )
}

function JellyfinPanel({ say }) {
  const [jf, setJf] = useState({ url: '', key_set: false })
  const [key, setKey] = useState('')
  useEffect(() => { api('/api/computeruse/jellyfin').then(setJf).catch(() => {}) }, [])
  async function save(e) {
    e.preventDefault()
    try {
      setJf(await api('/api/computeruse/jellyfin', {
        method: 'PUT', body: JSON.stringify({ url: jf.url, key }) }))
      setKey('')
      say('Jellyfin saved')
    } catch (err) { say(err.detail || String(err)) }
  }
  return (
    <section className="panel">
      <h2>Jellyfin</h2>
      <form className="row" onSubmit={save}>
        <input className="grow" placeholder="https://jellyfin.example"
               value={jf.url} onChange={(e) => setJf({ ...jf, url: e.target.value })} />
        <input type="password" placeholder={jf.key_set ? 'API key (stored)' : 'API key'}
               value={key} onChange={(e) => setKey(e.target.value)} />
        <button type="submit">Save</button>
      </form>
    </section>
  )
}

// --- the danger zone --------------------------------------------------------------

function DangerZone({ say }) {
  const [cf, setCf] = useState(null)          // { configured, revealed_until }
  const [now, setNow] = useState(Date.now() / 1000)
  const [dialog, setDialog] = useState(null)  // 'reveal' | 'rotate'
  const load = useCallback(
    () => api('/api/computeruse/cfaccess').then(setCf).catch(() => {}), [])
  useEffect(() => {
    load()
    const t = setInterval(() => { setNow(Date.now() / 1000); load() }, 5000)
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => { clearInterval(t); clearInterval(tick) }
  }, [load])

  const until = cf?.revealed_until
  const open = until && until > now

  return (
    <section className="panel danger-zone">
      <h2>Danger zone</h2>

      <div className="danger-row">
        <div className="grow">
          <strong>Make the Cloudflare token readable</strong>
          <p className="dim small">
            Testing only. Puts the Access secret back into the old set-up
            command on the Computer use tab, inline, for ten minutes on this
            host. Pairing gets it onto a machine without this; use it only for
            a machine the pairing routes cannot reach, and treat any terminal
            it is pasted into as holding the secret. Each opening is recorded
            in the Review Center.
          </p>
          {open && (
            <p className="warn">
              Readable for another {mmss(until - now)}.
            </p>
          )}
        </div>
        {open ? (
          <button className="ghost" onClick={async () => {
            await api('/api/computeruse/cfaccess/hide', { method: 'POST' })
            load()
          }}>Hide it now</button>
        ) : (
          <button className="ghost danger" disabled={cf && !cf.configured}
                  title={cf && !cf.configured ? 'no Access token is stored' : undefined}
                  onClick={() => setDialog('reveal')}>
            Make readable</button>
        )}
      </div>

      <div className="danger-row">
        <div className="grow">
          <strong>Rotate the pairing token</strong>
          <p className="dim small">
            Every connected machine drops on its next reconnect and has to be
            paired again. Do this if a machine was compromised or the token
            was seen somewhere it should not have been.
          </p>
        </div>
        <button className="ghost danger" onClick={() => setDialog('rotate')}>
          Rotate</button>
      </div>

      {dialog === 'reveal' && (
        <HoldToConfirm
          title="Make the Cloudflare Access secret readable?"
          body="For ten minutes the old set-up command will carry the secret in plain text. This is for testing only and will be recorded."
          confirmLabel="Hold to make readable"
          onClose={() => setDialog(null)}
          onConfirm={async (word) => {
            await api('/api/computeruse/cfaccess/reveal', {
              method: 'POST', body: JSON.stringify({ confirm: word }) })
            await load()
            say('The Cloudflare secret is readable for ten minutes')
          }} />
      )}
      {dialog === 'rotate' && (
        <HoldToConfirm
          title="Rotate the pairing token?"
          body="Every machine disconnects on its next reconnect and stays disconnected until it is paired again."
          confirmLabel="Hold to rotate"
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            await api('/api/computeruse/token', {
              method: 'POST', body: JSON.stringify({ rotate: true }) })
            say('Pairing token rotated — every machine needs pairing again')
          }} />
      )}
    </section>
  )
}

// Type the word, then hold the button for three seconds while a ring fills.
// A click does nothing; letting go early does nothing. Both halves are
// deliberate: the typed word proves attention, the hold proves intent, and
// neither can be satisfied by a reflex. The same shape guards every row above.
const HOLD_MS = 3000
const WORD = 'confirm'

function HoldToConfirm({ title, body, confirmLabel, onConfirm, onClose }) {
  const [word, setWord] = useState('')
  const [progress, setProgress] = useState(0)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const start = useRef(null)
  const raf = useRef(null)
  const done = useRef(false)
  const boxRef = useDismiss(!busy, onClose)      // outside pointerdown + Escape
  const armed = word.trim().toLowerCase() === WORD && !busy

  const stop = useCallback(() => {
    if (raf.current) cancelAnimationFrame(raf.current)
    raf.current = null
    start.current = null
    if (!done.current) setProgress(0)
  }, [])

  const tick = useCallback(async () => {
    if (start.current == null) return
    const p = Math.min(1, (performance.now() - start.current) / HOLD_MS)
    setProgress(p)
    if (p < 1) { raf.current = requestAnimationFrame(tick); return }
    // held all the way: fire once
    done.current = true
    start.current = null
    setBusy(true)
    try {
      await onConfirm(word.trim())
      onClose()
    } catch (e) {
      setErr(e.detail || String(e))
      done.current = false
      setBusy(false)
      setProgress(0)
    }
  }, [onConfirm, onClose, word])

  const begin = useCallback((e) => {
    if (!armed || done.current) return
    if (e.type === 'keydown' && (e.repeat || !['Enter', ' '].includes(e.key))) return
    if (e.type === 'keydown') e.preventDefault()
    setErr(null)
    start.current = performance.now()
    raf.current = requestAnimationFrame(tick)
  }, [armed, tick])

  useEffect(() => () => { if (raf.current) cancelAnimationFrame(raf.current) }, [])

  // ring geometry: r=15 → circumference ≈ 94.2
  const C = 2 * Math.PI * 15
  return (
    <div className="ask-scrim">
      <div className="ask-modal hold-modal" ref={boxRef} role="alertdialog" aria-modal="true">
        <div className="ask-body">
          <p className="ask-msg">{title}</p>
          <p className="ask-sub">{body}</p>
          <label className="hold-word">
            Type <code>{WORD}</code> to continue
            <input className="ask-input" value={word} autoFocus
                   autoComplete="off" spellCheck={false}
                   onChange={(e) => setWord(e.target.value)} />
          </label>
          {err && <p className="error">{err}</p>}
        </div>
        <div className="ask-foot">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            Cancel</button>
          <button type="button" className={`hold-btn danger${armed ? '' : ' off'}`}
                  disabled={!armed}
                  onPointerDown={begin} onPointerUp={stop} onPointerLeave={stop}
                  onPointerCancel={stop} onKeyDown={begin} onKeyUp={stop}
                  onContextMenu={(e) => e.preventDefault()}
                  aria-label={`${confirmLabel} — hold for three seconds`}>
            <svg className="hold-ring" viewBox="0 0 36 36" aria-hidden="true">
              <circle cx="18" cy="18" r="15" className="track" />
              <circle cx="18" cy="18" r="15" className="fill"
                      strokeDasharray={C} strokeDashoffset={C * (1 - progress)} />
            </svg>
            <span>{busy ? 'working…' : progress > 0 ? 'keep holding…' : confirmLabel}</span>
          </button>
        </div>
      </div>
    </div>
  )
}
