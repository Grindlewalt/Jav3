import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import Menu from '../components/Menu.jsx'

// The VM in plain words, at the sidebar's foot:
//
//   ● Sandbox ready · network: approved only · keeps: nothing
//
// "Sandbox", not VM/guest/overlay. Every fact comes from GET /api/vm/status —
// state from running/inflight/rebuilding/base_built, network from `egress`
// (off = netless, on = only through the approving proxy), and the wipe
// interval from `idle_scrub_seconds`. Nothing here is hardcoded.
//
// TODO(persistence): the sandbox keeps nothing today — the overlay is
// discarded on idle scrub or nuke. When the VM-persistence work lands it adds
// a field to /api/vm/status; read it in `keeps()` below (e.g. `approved
// changes`, or "3 changes waiting" as a link to where they are approved).

const POLL_MS = 15000

function state(s) {
  if (!s) return { word: 'unknown', tone: '' }
  if (s.rebuilding) return { word: 'rebuilding', tone: 'amber' }
  if (!s.base_built) return { word: 'not built', tone: 'amber' }
  if (s.running && s.inflight > 0) return { word: `working (${s.inflight})`, tone: 'live' }
  if (s.running) return { word: 'ready', tone: 'ok' }
  return { word: 'asleep', tone: '' }          // boots on the next turn
}

function keeps(s) {
  if (s?.persistence) return String(s.persistence.summary || s.persistence)
  return 'nothing'
}

function minutes(sec) {
  const m = Math.round(sec / 60)
  return m <= 1 ? 'a minute' : `${m} minutes`
}

export default function SandboxLine() {
  const [s, setS] = useState(null)
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  useEffect(() => {
    const load = () => api('/api/vm/status').then(setS).catch(() => setS(null))
    load()
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') load()
    }, POLL_MS)
    return () => clearInterval(t)
  }, [])
  if (!s) return null
  const st = state(s)
  const net = s.egress ? 'approved only' : 'off'
  return (
    <Menu open={open} onClose={close} up align="left" floating width={300}
          label="about the sandbox" wrapClassName="sh-sandbox-wrap"
          trigger={(
            <button type="button" className="sh-sandbox" aria-haspopup="dialog"
                    aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              <span className={`sh-dot ${st.tone}`} aria-hidden="true" />
              <span className="ellipsis">
                Sandbox {st.word} · network: {net} · keeps: {keeps(s)}</span>
            </button>
          )}>
      <div className="sh-sandbox-about">
        <p>Jav3 thinks and runs code inside a separate, disposable computer on
          this server. It holds no keys or passwords.</p>
        <p>{s.egress
          ? 'It reaches the internet only through a filter: a site you have not approved waits for you first.'
          : 'It has no network at all right now.'}</p>
        <p>{s.idle_scrub_seconds > 0
          ? `After ${minutes(s.idle_scrub_seconds)} idle it is wiped and starts clean next time.`
          : 'It is wiped when reset and starts clean next time.'}
          {' '}Project files live on the server, not in the sandbox, so they survive.</p>
      </div>
    </Menu>
  )
}
