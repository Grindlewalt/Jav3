import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { human } from './format.js'
import { notifyError } from './notify.js'
import { Button, Checkbox, Menu, Modal } from './components/index.js'

// Where the agent runs, in one line — the answer to "the VM is hard to
// understand". The agent's whole loop runs inside a throwaway virtual machine;
// the ONE thing that can survive it is a project's /persist disk, and only
// once the operator approved it here (PUT /api/projects/{slug}/persist, which
// the agent has no way to call).
//
//   <VmStrip slug="demo" />   Workspace header: strip + explainer + approve/revoke
//   <VmExplainer vm={status} /> the same three lines with no project (App's VM chip)
//
// Every figure comes from the server: the scrub window from /api/vm/status,
// the disk cap and usage from the persist endpoint. Nothing here is a constant
// that could go stale.

// the disk cap is a round number of GB; human() tops out at MB
function size(n) {
  const gb = (Number(n) || 0) / 1024 ** 3
  return gb >= 1 ? `${Number.isInteger(gb) ? gb : gb.toFixed(1)} GB` : human(n)
}

function minutes(sec) {
  const m = Math.round(sec / 60)
  return m <= 1 ? 'a minute' : `${m} minutes`
}

// "fresh each session" is only true when idle scrub is on; with it off the
// guest lives until it is nuked or the app restarts, and saying otherwise
// would be exactly the confusion this component exists to remove.
export function freshness(vm) {
  const scrub = vm?.idle_scrub_seconds || 0
  return scrub > 0
    ? { short: 'fresh each session', when: `after ${minutes(scrub)} idle` }
    : { short: 'reset on restart', when: 'when it restarts or you nuke it' }
}

// The three lines: what is disposable, what persists, what was approved.
// `persist` is the project's persist view (or null outside a project).
export function VmExplainer({ vm, persist }) {
  const fresh = freshness(vm)
  const on = !!persist?.approved && persist?.enabled !== false
  const mount = persist?.mount || vm?.persist?.mount || '/persist'
  let persists
  if (!persist) {
    persists = `Only ${mount}, and only for a project you approve.`
  } else if (!on) {
    persists = 'Nothing. This project has no disk in the VM.'
  } else {
    const d = persist.disk || {}
    persists = `${mount}: this project's own disk, up to ${size(d.cap_bytes)}`
      + (d.exists ? ` (${size(d.bytes_used)} used)` : '')
      + '. It turns read-only once a session reads the web.'
  }
  let approved
  if (!persist) {
    const holder = vm?.persist?.holder
    approved = holder ? `${mount} is attached for ${holder} right now.`
                      : 'You approve it per project, from its workspace.'
  } else if (persist.enabled === false) {
    approved = 'Persistence is switched off on the server.'
  } else if (on) {
    approved = `You approved it${persist.approved_at
      ? ` on ${String(persist.approved_at).slice(0, 10)}` : ''}. The agent can't change this.`
  } else {
    approved = "You haven't approved it. The agent can't turn it on."
  }
  return (
    <dl className="vm-explain">
      <dt>Disposable</dt>
      <dd>Everything the agent installs or writes in the VM, wiped {fresh.when}.
        Project files are copied in each turn and edits come back through the file tools.</dd>
      <dt>Persists</dt>
      <dd>{persists}</dd>
      <dt>Approved</dt>
      <dd>{approved}</dd>
    </dl>
  )
}

export default function VmStrip({ slug }) {
  const [vm, setVm] = useState(null)
  const [persist, setPersist] = useState(null)
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState(null)      // 'approve' | 'revoke' | null
  const [wipe, setWipe] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api('/api/vm/status').then(setVm).catch(() => setVm(null))
    if (slug) {
      api(`/api/projects/${slug}/persist`).then(setPersist).catch(() => setPersist(null))
    }
  }, [slug])
  useEffect(() => { load() }, [load])
  // re-read when the explainer opens: approval, usage and the attached state
  // all move while the page sits open
  useEffect(() => { if (open) load() }, [open, load])
  const close = useCallback(() => setOpen(false), [])

  async function save(approved) {
    setBusy(true)
    try {
      const r = await api(`/api/projects/${slug}/persist`, {
        method: 'PUT',
        body: JSON.stringify(approved
          ? { approved: true, acknowledge: true }
          : { approved: false, delete_disk: wipe }),
      })
      setPersist(r)
      if (r.delete_error) notifyError(new Error(r.delete_error))
      setDialog(null)
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  const on = !!persist?.approved && persist?.enabled !== false
  const mount = persist?.mount || '/persist'
  const fresh = freshness(vm)
  const trigger = (
    <button type="button" className="vm-strip-btn" aria-haspopup="dialog"
            aria-expanded={open} onClick={() => setOpen((o) => !o)}
            title="where the agent runs, and what survives">
      <span className={`run-dot ${vm?.running ? 'running' : ''}`} aria-hidden="true" />
      <span>Runs in a VM</span>
      <span className="vm-strip-sep" aria-hidden="true">·</span>
      <span className="vm-strip-fresh">{fresh.short}</span>
      <span className="vm-strip-sep" aria-hidden="true">·</span>
      <span className={on ? 'vm-strip-on' : 'dim'}>
        persistence: {on ? `on (${mount})` : 'off'}</span>
    </button>
  )
  return (
    <>
      <Menu open={open} onClose={close} trigger={trigger} align="left" width={340}
            floating label="about the VM" wrapClassName="vm-strip">
        <VmExplainer vm={vm} persist={slug ? persist : null} />
        {slug && persist && persist.enabled !== false && (
          <div className="vm-explain-foot">
            {on
              ? <Button variant="ghost" onClick={() => { setWipe(false); setDialog('revoke'); close() }}>
                  Revoke persistence…</Button>
              : <Button variant="ghost" onClick={() => { setDialog('approve'); close() }}>
                  Allow persistence…</Button>}
          </div>
        )}
      </Menu>

      <Modal open={dialog === 'approve'} onClose={() => setDialog(null)}
             title="Allow persistence in the VM?" onSubmit={() => save(true)}
             footer={<>
               <Button variant="ghost" onClick={() => setDialog(null)}>Cancel</Button>
               <Button type="submit" disabled={busy}>Allow</Button>
             </>}>
        <p>The agent runs in a virtual machine that is wiped {fresh.when}. Allowing
          persistence gives this project one exception: a disk
          at <code>{mount}</code> of up to {size(persist?.disk?.cap_bytes)} that
          survives between sessions.</p>
        <p>Anything the agent saves there can come back in a later session, including
          something a web page tricked it into writing. To limit that, the disk
          can't run programs directly, turns read-only for the rest of a session
          once the agent reads the web, and is only attached while this project is working.</p>
        <p className="dim">You can revoke this any time and delete the disk. Only you
          can turn it on; the agent can't.</p>
      </Modal>

      <Modal open={dialog === 'revoke'} onClose={() => setDialog(null)}
             title="Revoke persistence?" onSubmit={() => save(false)}
             footer={<>
               <Button variant="ghost" onClick={() => setDialog(null)}>Cancel</Button>
               <Button type="submit" danger disabled={busy}>Revoke</Button>
             </>}>
        <p>From the next session the agent gets no <code>{mount}</code> disk. A session
          running now keeps it until it ends.</p>
        {persist?.disk?.exists && (
          <Checkbox checked={wipe} onChange={(e) => setWipe(e.target.checked)}
                    label={`Also delete the disk and everything on it (${size(persist.disk.bytes_used)} used)`} />
        )}
      </Modal>
    </>
  )
}
