import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { notify, notifyError } from './notify.js'
import { Button, Modal, Select } from './components/index.js'
import { approvePending, rejectPending } from './boxes/api/policy.js'
import { needsProject, projectLabel } from './boxes/logic.js'
import { PICK_BODY, PICK_TITLE, decidedText } from './securityCopy.js'

// One way to answer a host a box asked for, shared by the Network tab's
// "Waiting for you" and the Queue's "Egress hosts" so the two speak the same
// words (WEB-07): Allow always, Allow 1 h, Deny.
//
//   const { decide, picker, choose } = useEgressDecide(onDone)
//   ... onClick={() => decide(row, 'allow')}   // 'allow' | 'once' | 'deny'
//   ... {picker}                               // render once
//
// A row that came from no project (the shared box, no turn) needs a project to
// go on; the host answers 409 without one. `picker` is the dialog that asks,
// with a list to choose from instead of a typed slug.
//
// The host and every name here are guest-supplied: text nodes only.

const NO_NAMES = {}

export function useEgressDecide(onDone, { project = null, names = NO_NAMES } = {}) {
  const [ask, setAsk] = useState(null)          // {row, verb, resolve}
  const pending = useRef(null)

  const pick = useCallback((row, verb) => new Promise((resolve) => {
    pending.current = resolve
    setAsk({ row, verb })
  }), [])
  const settle = useCallback((slug) => {
    const r = pending.current
    pending.current = null
    setAsk(null)
    if (r) r(slug)
  }, [])
  // never leave a caller awaiting a dialog that has gone
  useEffect(() => () => { if (pending.current) pending.current(null) }, [])

  const decide = useCallback(async (row, verb) => {
    try {
      if (verb === 'deny') {
        await rejectPending(row.id)
        notify(decidedText(row.host, 'deny'), { life: 6 })
      } else {
        let proj = project || null
        if (needsProject(row) && !proj) {
          proj = await pick(row, verb)
          if (!proj) return
        }
        await approvePending(row.id, needsProject(row) ? proj : null, { once: verb === 'once' })
        const slug = needsProject(row) ? proj : (row.project_slug || row.project)
        notify(decidedText(row.host, verb, projectLabel(slug, names)), { life: 6 })
      }
      onDone?.()
    } catch (err) { notifyError(err) }
  }, [onDone, pick, project, names])

  const picker = ask && (
    <ProjectPicker key={`${ask.row.id}:${ask.verb}`} host={ask.row.host} verb={ask.verb}
                   onPick={settle} />
  )
  // a project for a host that is not a queue row (an auto-deny to allow anyway)
  const choose = useCallback((host, verb = 'allow') => pick({ id: host, host }, verb), [pick])
  return { decide, picker, choose }
}

function ProjectPicker({ host, verb, onPick }) {
  const [slugs, setSlugs] = useState(null)
  const [names, setNames] = useState({})
  const [slug, setSlug] = useState('')
  useEffect(() => {
    api('/api/projects').then((r) => {
      const ps = (r.projects || []).filter((p) => !p.slug.startsWith('__'))
      setSlugs(ps.map((p) => p.slug))
      setNames(Object.fromEntries(ps.map((p) => [p.slug, p.name])))
      if (ps.length === 1) setSlug(ps[0].slug)
    }).catch(() => setSlugs([]))
  }, [])
  return (
    <Modal title={PICK_TITLE(verb)} width={440} onClose={() => onPick(null)}
           onSubmit={() => slug && onPick(slug)}
           footer={<>
             <Button variant="ghost" onClick={() => onPick(null)}>Cancel</Button>
             <Button type="submit" disabled={!slug}>
               {verb === 'once' ? 'Allow 1 h' : 'Allow always'}</Button>
           </>}>
      <p className="small">{PICK_BODY(host, verb)}</p>
      {slugs === null && <div className="dim small">…</div>}
      {slugs && slugs.length === 0 && (
        <div className="dim small">No projects yet, so there is nothing to attach it to.</div>)}
      {slugs && slugs.length > 0 && (
        <Select aria-label="project" value={slug} placeholder="Choose a project"
                onChange={(e) => setSlug(e.target.value)}
                options={slugs.map((s) => ({ value: s, label: names[s] || s }))} />
      )}
    </Modal>
  )
}
