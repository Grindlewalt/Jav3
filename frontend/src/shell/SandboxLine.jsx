import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import Menu from '../components/Menu.jsx'
import { VmExplainer } from '../VmStrip.jsx'

// The VM in plain words, at the sidebar's foot:
//
//   ● Sandbox ready · network: approved only · keeps: nothing
//
// "Sandbox", not VM/guest/overlay. Every fact comes from the server: state
// from /api/vm/status (running / inflight / rebuilding / base_built), network
// from its `egress` flag (off = netless, on = only through the approving
// proxy), and "keeps" from the open project's /persist approval
// (GET /api/projects/{slug}/persist) — a project-less chat keeps nothing.
// The popover is VmStrip's three lines (disposable / persists / approved), so
// the shell and the Workspace header explain the VM the same way; approving
// or revoking /persist stays in the Workspace header, where the modal lives.

const POLL_MS = 15000

function state(s) {
  if (s.rebuilding) return { word: 'rebuilding', tone: 'amber' }
  if (!s.base_built) return { word: 'not built', tone: 'amber' }
  if (s.running && s.inflight > 0) return { word: `working (${s.inflight})`, tone: 'live' }
  if (s.running) return { word: 'ready', tone: 'ok' }
  return { word: 'asleep', tone: '' }          // boots on the next turn
}

export default function SandboxLine({ slug }) {
  const [vm, setVm] = useState(null)
  const [persist, setPersist] = useState(null)
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])

  useEffect(() => {
    const load = () => api('/api/vm/status').then(setVm).catch(() => setVm(null))
    load()
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') load()
    }, POLL_MS)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    setPersist(null)
    if (!slug) return
    api(`/api/projects/${encodeURIComponent(slug)}/persist`)
      .then(setPersist).catch(() => setPersist(null))
  }, [slug, open])

  if (!vm) return null
  const st = state(vm)
  const net = vm.egress ? 'approved only' : 'off'
  const kept = persist?.approved && persist?.enabled !== false
  const keeps = kept ? (persist.mount || vm.persist?.mount) : 'nothing'
  return (
    <Menu open={open} onClose={close} up align="left" floating width={320}
          label="about the sandbox" wrapClassName="sh-sandbox-wrap"
          trigger={(
            <button type="button" className="sh-sandbox" aria-haspopup="dialog"
                    aria-expanded={open} onClick={() => setOpen((o) => !o)}
                    title="where the agent runs, and what survives">
              <span className={`sh-dot ${st.tone}`} aria-hidden="true" />
              <span className="ellipsis">
                Sandbox {st.word} · network: {net} · keeps: {keeps}</span>
            </button>
          )}>
      <VmExplainer vm={vm} persist={slug ? persist : null} />
      <p className="sh-sandbox-net">
        {vm.egress
          ? 'Network: only through this server’s filter. A site not on the allowlist is held for approval.'
          : 'Network: none. The sandbox cannot reach the internet right now.'}</p>
    </Menu>
  )
}
