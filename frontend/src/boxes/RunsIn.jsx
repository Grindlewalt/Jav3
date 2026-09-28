import { useEffect, useMemo, useState } from 'react'
import { Button, Input, Select, Tag } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { getPlacement, setPlacement } from './api/placement.js'
import { startBox } from './api/vms.js'
import {
  effectiveText, inUseBoxes, memError, newBoxBody, pickerItems, pickOutcome, runtimeLabel,
  separateBody,
} from './placement.js'

// A project's "Runs in" (backend/vm/placement.py): the box its turns run in.
// The profile's setting is only the default; this overrides it. Operator
// only (the API is cookie-session gated). Applies from the next turn.
//
//   Boxes in use now            [Use this] per box
//   [ Pick any box or image… ]  [+ New box]
//   picking another project's box asks: Share it, or Separate (a new box
//   from the same image)
export default function RunsIn({ slug }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState(null)       // {box} awaiting Share/Separate
  const [choice, setChoice] = useState('separate')
  const [mem, setMem] = useState('')
  const [runtime, setRuntime] = useState('kvm')
  const [newBox, setNewBox] = useState(null)       // {runtime, image, mem} or null
  const [busy, setBusy] = useState(false)
  const [warnings, setWarnings] = useState([])
  const ask = useAsk()

  function load() {
    getPlacement(slug).then((d) => { setData(d); setErr(null) })
      .catch((e) => { setData(null); setErr(e) })
  }
  useEffect(() => { setPicked(null); setNewBox(null); setWarnings([]); load() }, [slug]) // eslint-disable-line

  const items = useMemo(() => (data ? pickerItems(data.boxes, data.images, query) : []),
    [data, query])
  if (err) return null        // not the operator, or an older server: no section
  if (!data) return null

  const eff = data.effective
  const dockerOk = !!data.docker_enabled

  async function send(body, { confirmJoin = false, start = false } = {}) {
    if (confirmJoin && !await ask.confirm(`Share ${body.box_id} with ${slug}?`, {
      body: data.join_warning, confirmLabel: 'Share', danger: true })) return
    setBusy(true)
    try {
      const r = await setPlacement(slug, body)
      setData(r); setWarnings(r.warnings || []); setPicked(null); setNewBox(null)
      notify(`${slug} runs in ${effectiveText(r.effective, r.profile?.name)} from its next turn`)
      if (start && r.enabled && r.effective?.mode === 'own') {
        // [Start]: boot it now rather than at the next turn (caps may refuse;
        // the choice is saved either way)
        startBox(r.effective.box_id).then(load).catch(notifyError)
      }
    } catch (e) { notifyError(e) }
    setBusy(false)
  }

  function chooseBox(box) {
    const o = pickOutcome(box, slug, eff)
    if (o.kind === 'current' || o.kind === 'none') return
    if (o.kind === 'ask') {
      setPicked({ box, users: o.users }); setChoice('separate')
      setRuntime(box.runtime || 'kvm'); setMem(''); setNewBox(null)
      return
    }
    send(o.body)
  }

  function pickImage(im) {
    setPicked(null)
    setNewBox({ runtime: 'kvm', image: im.name, mem: '' })
  }

  const memErr = memError(picked ? mem : newBox?.mem)
  const rtOptions = [{ value: 'kvm', label: 'VM' },
    ...(dockerOk ? [{ value: 'docker', label: 'container' }] : [])]

  return (
    <div className="sbx-card bx-runsin">
      <div className="sbx-sec-head"><h3>Runs in</h3>
        {eff.source === 'project' && (
          <Button variant="ghost" disabled={busy}
                  title="forget this project's choice and use its profile's default"
                  onClick={() => send({ mode: 'profile' })}>Use profile default</Button>)}
      </div>
      <div className="small">Now: <b>{effectiveText(eff, data.profile?.name)}</b>
        {!data.enabled && <span className="dim"> · boxes are off on this server: every
          project runs in the shared box until they are on</span>}</div>
      {warnings.map((w) => <div key={w} className="error small">{w}</div>)}

      <div className="dim small bx-runsin-label">Boxes in use now</div>
      <ul className="staged-list rev-list">
        {inUseBoxes(data.boxes).map((b) => (
          <li key={b.id}>
            <span className={`bx-dot ${b.state === 'running' ? 'on' : ''}`}>●</span>
            <span className="grow">
              <span className="mono">{b.id}</span>
              {' '}<span className="dim small">{b.image} · {runtimeLabel(b.runtime)} · {b.state}
                {b.used_by?.length ? ` · ${b.used_by.join(', ')}` : ''}</span>
            </span>
            {b.id === eff.box_id ? <Tag>current</Tag> : (
              <Button variant="ghost" disabled={busy} onClick={() => chooseBox(b)}>Use this</Button>)}
          </li>
        ))}
      </ul>

      <div className="row">
        <Input className="grow" placeholder="Pick any box or image…" value={query}
               aria-label="filter boxes and images" onChange={(e) => setQuery(e.target.value)} />
        <Button variant="ghost" disabled={busy}
                onClick={() => { setPicked(null); setNewBox({ runtime: 'kvm', image: 'main', mem: '' }) }}>
          + New box</Button>
      </div>
      {query && (
        <ul className="staged-list rev-list bx-runsin-pick">
          {items.length === 0 && <li className="dim small">nothing matches</li>}
          {items.map((it) => (it.type === 'box' ? (
            <li key={it.key}>
              <span className="grow"><span className="mono">{it.box.id}</span>
                {' '}<span className="dim small">{it.box.image} · {runtimeLabel(it.box.runtime)} · {it.box.state}
                  {it.box.used_by?.length ? ` (in use: ${it.box.used_by.join(', ')})` : ''}</span></span>
              {it.box.id === eff.box_id ? <Tag>current</Tag> : (
                <Button variant="ghost" disabled={busy} onClick={() => chooseBox(it.box)}>Pick</Button>)}
            </li>
          ) : (
            <li key={it.key}>
              <span className="grow">image: <span className="mono">{it.image.name}</span>
                {' '}<span className="dim small">{it.running ? '(a box runs it)' : '(not running)'}</span></span>
              <Button variant="ghost" disabled={busy} onClick={() => pickImage(it.image)}>New box</Button>
            </li>
          )))}
        </ul>
      )}

      {picked && (
        <div className="bx-runsin-ask">
          <div className="small">You picked <span className="mono">{picked.box.id}</span>
            {picked.users.length ? ` (in use by ${picked.users.join(', ')})` : ''}:</div>
          <label className="check-row">
            <input type="radio" name={`runsin-${slug}`} checked={choice === 'share'}
                   onChange={() => setChoice('share')} />
            <span>Share it with {picked.users.join(', ') || 'its project'}</span>
          </label>
          {choice === 'share' && <div className="error small">{data.join_warning}</div>}
          <label className="check-row">
            <input type="radio" name={`runsin-${slug}`} checked={choice === 'separate'}
                   onChange={() => setChoice('separate')} />
            <span>Separate: a new box from the same image ({picked.box.image})</span>
          </label>
          {choice === 'separate' && (
            <div className="row">
              <Select label="Runtime" value={runtime} options={rtOptions}
                      onChange={(e) => setRuntime(e.target.value)} />
              <Input label="Memory (MB)" type="number" min={256} step={64} value={mem}
                     placeholder="profile's" error={memErr} onChange={(e) => setMem(e.target.value)} />
            </div>
          )}
          <div className="row">
            <Button variant="ghost" onClick={() => setPicked(null)}>Cancel</Button>
            <Button disabled={busy || (choice === 'separate' && !!memErr)}
                    danger={choice === 'share'}
                    onClick={() => (choice === 'share'
                      ? send({ mode: 'join', box_id: picked.box.id }, { confirmJoin: true })
                      : send({ ...separateBody(picked.box, mem), runtime }, { start: true }))}>
              {choice === 'share' ? 'Share' : 'Start'}</Button>
          </div>
        </div>
      )}

      {newBox && (
        <div className="bx-runsin-ask">
          <div className="small">A box of its own for {slug} (<span className="mono">p-{slug}</span>):</div>
          <div className="row">
            <Select label="Runtime" value={newBox.runtime} options={rtOptions}
                    onChange={(e) => setNewBox({ ...newBox, runtime: e.target.value })} />
            <Select label="Image" value={newBox.image}
                    options={[...new Set([...data.images.map((i) => i.name), newBox.image])]}
                    onChange={(e) => setNewBox({ ...newBox, image: e.target.value })} />
            <Input label="Memory (MB)" type="number" min={256} step={64} value={newBox.mem}
                   placeholder="profile's" error={memErr}
                   onChange={(e) => setNewBox({ ...newBox, mem: e.target.value })} />
          </div>
          {data.budget?.ram_mb_cap && (
            <span className="field-hint">Boxes in use: {data.budget.ram_mb_used} of about
              {' '}{data.budget.ram_mb_cap} MB; project boxes {data.budget.project_boxes}
              /{data.budget.project_boxes_cap}.</span>)}
          <div className="row">
            <Button variant="ghost" onClick={() => setNewBox(null)}>Cancel</Button>
            <Button disabled={busy || !!memErr} onClick={() => send(newBoxBody(newBox), { start: true })}>Start</Button>
          </div>
        </div>
      )}
      <div className="dim small">Applies from this project's next turn. Egress rules,
        secrets and LAN access stay this project's wherever it runs.</div>
    </div>
  )
}
