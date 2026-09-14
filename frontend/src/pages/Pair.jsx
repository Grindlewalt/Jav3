/* The confirm page: /pair/XXXX-XXXX.
 *
 * A client (a computer being paired, or a device/CLI being authorized) prints
 * this address after it claims a code, and the operator opens it in a browser
 * where they are logged in to Jarvis. What it shows is WHICH machine is asking —
 * the name it gave, its hostname and platform, where from and when — because the
 * one thing the host cannot know is whether the machine that claimed the code is
 * the one the operator is sitting at. The operator knows. This is where they say.
 *
 * One page serves both flows. The code alone doesn't say which, so useTicket
 * probes both enroll endpoints; the ticket's `kind` then drives the copy and the
 * approve/deny endpoint (computer-use pairing vs. generic device authorization).
 */
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'

const BASE = { device: '/api/devices', computeruse: '/api/computeruse' }
const baseFor = (t) => BASE[t && t.kind] || '/api/computeruse'

// Polls the ticket while the page is open. `gone` means Jarvis no longer has it:
// expired, spent, or never issued — the API deliberately does not say which, so
// neither does this. Tries the device namespace first, then computer-use.
export function useTicket(code, every = 2000) {
  const [t, setT] = useState(null)
  const [gone, setGone] = useState(false)
  useEffect(() => {
    if (!code) return undefined
    let live = true
    const c = encodeURIComponent(code)
    const load = () => api(`/api/devices/enroll/${c}`)
      .catch((err) => {
        if (err.status === 404) return api(`/api/computeruse/enroll/${c}`)
        throw err
      })
      .then((r) => { if (live) { setT(r); setGone(false) } })
      .catch((err) => { if (live && err.status === 404) setGone(true) })
    load()
    const id = setInterval(load, every)
    return () => { live = false; clearInterval(id) }
  }, [code, every])
  return [t, gone, setT]
}

const when = (ts) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  return s < 5 ? 'just now' : s < 90 ? `${s}s ago` : `${Math.round(s / 60)} min ago`
}

const plat = (p) => (p === 'darwin' ? 'macOS' : p === 'win32' ? 'Windows' : p || '?')

// One machine's claim, and the two answers to it.
export function ClaimCard({ ticket, onChange, compact = false }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const c = ticket.claim || {}
  const contested = ticket.contested || []
  const isDevice = ticket.kind === 'device'
  const thing = isDevice ? 'device' : 'machine'

  async function answer(verb) {
    setBusy(true)
    setErr(null)
    try {
      onChange(await api(
        `${baseFor(ticket)}/enroll/${encodeURIComponent(ticket.code)}/${verb}`,
        { method: 'POST' }))
    } catch (e) { setErr(e.detail || String(e)) } finally { setBusy(false) }
  }

  if (ticket.state === 'waiting') {
    return (
      <div className="pair-card waiting">
        <p><strong>Waiting for the {thing}.</strong></p>
        <p className="dim small">
          Nothing has claimed code <code>{ticket.code}</code> yet. Run the
          command on the {thing}; this fills in the moment it does.
          {ticket.expires_in > 0 && ` The code is good for another ${
            Math.ceil(ticket.expires_in / 60)} min.`}
        </p>
      </div>
    )
  }
  if (ticket.state === 'denied') {
    return (
      <div className="pair-card">
        <p><strong>Denied.</strong> <span className="dim">The {thing} was told,
          and nothing was handed over. Make a new code to try again.</span></p>
      </div>
    )
  }
  if (ticket.state === 'approved' || ticket.state === 'released') {
    return (
      <div className="pair-card">
        <p className="badge">✓ Confirmed
          {ticket.state === 'released'
            && (isDevice ? ' — the device has its token' : ' — the machine has its credentials')}</p>
        <p className="dim small">
          {ticket.state === 'released'
            ? 'It is connecting now. '
            : `It picks ${isDevice ? 'the token' : 'them'} up within a few seconds. `}
          {!compact && (isDevice
            ? <Link to="/settings">Manage it under Devices in Settings.</Link>
            : <Link to="/computer">Watch for it on the Computer use tab.</Link>)}
        </p>
      </div>
    )
  }
  // claimed: the decision
  return (
    <div className="pair-card">
      <p>
        <strong>{c.name || ticket.name || (isDevice ? 'A device' : 'A machine')}</strong>
        <span className="dim"> wants {isDevice ? 'API access' : 'to pair'} with code </span>
        <code>{ticket.code}</code>
      </p>
      <dl className="pair-facts">
        <dt>hostname</dt><dd>{c.hostname || <span className="dim">not given</span>}</dd>
        <dt>platform</dt><dd>{plat(c.platform)}</dd>
        <dt>from</dt><dd>{c.peer || '?'}</dd>
        <dt>claimed</dt><dd>{c.at ? when(c.at) : '?'}</dd>
      </dl>
      {contested.length > 0 && (
        <p className="warn">
          {contested.length === 1 ? 'Another machine' : `${contested.length} other machines`}
          {' '}also tried to claim this code
          {' '}({contested.map((x) => x.hostname || x.peer || '?').join(', ')}).
          Jarvis kept the first claim, shown above. If that is not the {thing}
          you are setting up, deny this and make a new code.
        </p>
      )}
      <p className="dim small">
        Confirm only if this is the {thing} you just ran the command on. Saying
        yes {isDevice
          ? 'issues it a revocable API token for Jarvis.'
          : 'hands it the pairing token and the Cloudflare Access service token.'}
      </p>
      {err && <p className="error">{err}</p>}
      <div className="row">
        <button disabled={busy} onClick={() => answer('approve')}>
          Confirm this {thing}</button>
        <button className="ghost danger" disabled={busy}
                onClick={() => answer('deny')}>Deny</button>
      </div>
    </div>
  )
}

export default function Pair() {
  const { code } = useParams()
  const [ticket, gone, setTicket] = useTicket(code)
  const isDevice = ticket && ticket.kind === 'device'

  return (
    <div className="page pair-page">
      <h1>{isDevice ? 'Authorize a device' : 'Pair a computer'}</h1>
      {gone ? (
        <section className="panel">
          <p><strong>This code is not live.</strong></p>
          <p className="dim small">
            It expired, it was already used, or it was never issued — codes
            last fifteen minutes and work once. Make a new one and run the
            command again.
          </p>
        </section>
      ) : !ticket ? (
        <p className="dim">loading…</p>
      ) : (
        <section className="panel">
          <ClaimCard ticket={ticket} onChange={setTicket} />
        </section>
      )}
    </div>
  )
}
