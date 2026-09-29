// Small shared pieces for the boxes pages. Every guest- or agent-supplied
// string they render is a plain text node.
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Modal, Tag } from '../components/index.js'
import { missing } from './http.js'
import { dockerLead } from '../securityCopy.js'

// Load a thing, poll it, and tell "the backend has no such route yet"
// (`unavailable`) apart from an ordinary failure (`error`).
export function useLoad(fn, { every = 0, deps = [] } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [unavailable, setUnavailable] = useState(false)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const reload = useCallback(() => fnRef.current()
    .then((d) => { setData(d); setError(null); setUnavailable(false); return d })
    .catch((e) => {
      if (missing(e)) setUnavailable(true)
      else setError(e)
      return null
    }), [])
  useEffect(() => {
    reload()
    if (!every) return undefined
    const t = setInterval(reload, every)
    return () => clearInterval(t)
  }, [reload, every, ...deps]) // eslint-disable-line react-hooks/exhaustive-deps
  return { data, error, unavailable, reload, setData }
}

export function Unavailable({ what }) {
  return (
    <div className="bx-unavail dim small">
      {what} is not available on this server yet — the backend for it has not
      landed. (Add <code>?mock=1</code> to the address to preview it with sample data.)
    </div>
  )
}

export function LoadError({ error }) {
  if (!error) return null
  return <div className="bx-unavail error small">{error.detail || String(error)}</div>
}

export function StateDot({ state, inflight }) {
  const cls = state === 'running' ? (inflight > 0 ? 'running' : 'done')
    : state === 'failed' ? 'error' : ''
  return <span className={`run-dot ${cls}`} aria-hidden="true" />
}

export function StateTag({ state }) {
  const tone = { running: 'done', stopped: undefined, failed: 'error', unreported: 'pending' }[state]
  return <Tag tone={tone}>{state || '?'}</Tag>
}

// Docker boxes share the host kernel: said on every row, not in a footnote.
export function RuntimeTag({ runtime }) {
  if (runtime === 'docker') {
    return (
      <span className="bx-runtime">
        <Tag>docker</Tag>
        <Tag className="bx-less" title="a container shares the host kernel; a KVM box has its own">
          less isolated</Tag>
      </span>
    )
  }
  return <Tag>{runtime || 'kvm'}</Tag>
}

// What the host can run boxes on (GET /api/vm/boxes `runtimes`, normalised by
// logic.normRuntimes): docker greyed out with the server's reason when it is
// unavailable, and its "weak isolation" warnings shown whenever `weak`.
export function RuntimeStatus({ runtimes, compact = false }) {
  if (!runtimes) return null
  const { kvm, docker } = runtimes
  const flags = ['rootless', 'userns', 'gvisor', 'seccomp'].filter((k) => docker[k])
  return (
    <div className={compact ? 'bx-runtimes small' : 'sbx-card bx-runtimes small'}>
      <div className={kvm.available ? '' : 'bx-runtime-off'}>
        <b>KVM</b> {kvm.available ? <Tag tone="done">available</Tag> : <Tag tone="error">unavailable</Tag>}
        {kvm.reason && <span className="dim"> — {kvm.reason}</span>}
      </div>
      <div className={docker.available ? '' : 'bx-runtime-off'}>
        <b>Docker</b> {docker.available ? <Tag tone="done">available</Tag> : <Tag>unavailable</Tag>}
        {!docker.available && docker.reason && <span className="dim"> — {docker.reason}</span>}
        {docker.available && flags.length > 0 && <span className="dim"> · {flags.join(' · ')}</span>}
      </div>
      {docker.available && (docker.weak || docker.warnings.length > 0) && (
        <div className="bx-warn-box">
          <span className="warn">{dockerLead(docker)}</span>
          <details className="bx-warn-details">
            <summary className="dim">Details</summary>
            {docker.weak && <div className="warn"><b>Weak isolation</b>: a docker box would run
              without a user namespace.</div>}
            {docker.warnings.length > 0 && (
              <ul className="bx-warnings">
                {docker.warnings.map((w, i) => <li key={i} className="warn">{w}</li>)}
              </ul>)}
          </details>
        </div>
      )}
    </div>
  )
}

export function PlacementTag({ placement }) {
  if (!placement) return null
  return <Tag className="bx-place" title="service placement">{placement.replace('_', ' ')}</Tag>
}

// A confirm with room to say exactly what happens, and an optional checkbox
// (destroy's "also delete the data disk"). onConfirm receives the checkbox.
export function Confirm({
  open, title, children, confirmLabel = 'Confirm', danger = false, check, onConfirm, onClose,
}) {
  const [checked, setChecked] = useState(false)
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (open) { setChecked(false); setBusy(false) } }, [open])
  if (!open) return null
  const go = async () => {
    setBusy(true)
    try { await onConfirm(checked) } finally { setBusy(false) }
  }
  return (
    <Modal open title={title} onClose={onClose} onSubmit={go} width={520}
           footer={<>
             <Button variant="ghost" onClick={onClose}>Cancel</Button>
             <Button type="submit" danger={danger} disabled={busy}>{confirmLabel}</Button>
           </>}>
      <div className="bx-confirm">{children}</div>
      {check && (
        <label className="check-row">
          <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
          <span>{check}</span>
        </label>
      )}
    </Modal>
  )
}

export function ProjectList({ slugs, empty = 'no project' }) {
  if (!slugs || !slugs.length) return <span className="dim">{empty}</span>
  return (
    <span className="bx-projlist">
      {slugs.map((s) => <Tag key={s}>{s}</Tag>)}
    </span>
  )
}
