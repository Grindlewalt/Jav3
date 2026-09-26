import { useEffect, useMemo, useState } from 'react'
import { Button, EmptyState, Input, Select, Tag } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import {
  assignProfile, createProfile, deleteProfile, listProfiles, secretNames, updateProfile,
} from '../boxes/api/profiles.js'
import { listImages } from '../boxes/api/images.js'
import { listProjects } from '../boxes/api/persist.js'
import {
  blankProfile, parseHosts, PLACEMENTS, profilePayload, RUNTIMES, validateProfile,
} from '../boxes/logic.js'
import { LoadError, ProjectList, Unavailable, useLoad } from '../boxes/ui.jsx'

// Security > Profiles: a project's whole security posture in one named row —
// which secrets it may have, how its egress is judged, whether its alerts are
// auto-handled, and the box it runs in. Built-in profiles are read-only here
// (duplicate one to change it). Every project has exactly one profile.
//
// Placement and runtime have NO default for a new profile (operator decision
// 0.1 and the docker addendum): the form will not save until both are picked.

const NEVER_AUTO = 'service_*, package_*, unexpected_process and proc_report_mismatch '
  + 'are never auto-handled, whatever this says; nor is anything on the reviewer\'s own never-list.'

export default function Profiles() {
  const pr = useLoad(listProfiles)
  const [editing, setEditing] = useState(null)       // a profile object (id null = new)
  const [secrets, setSecrets] = useState([])
  const [variants, setVariants] = useState(['main', 'dev', 'desktop'])
  const [projects, setProjects] = useState([])
  const ask = useAsk()

  useEffect(() => {
    secretNames().then(setSecrets).catch(() => {})
    listImages().then((r) => { if (r.variants.length) setVariants(r.variants.map((v) => v.name)) })
      .catch(() => {})
    listProjects().then(setProjects).catch(() => {})
  }, [])

  async function remove(p) {
    const n = (p.projects || []).length
    if (!await ask.confirm(`Delete profile ${p.name}?`, {
      body: n ? `${n} project(s) use it and move to Default.` : 'No project uses it.',
      confirmLabel: 'Delete', danger: true })) return
    try { await deleteProfile(p.id); pr.reload() } catch (e) { notifyError(e) }
  }

  if (pr.unavailable) return <div className="bx-page"><Unavailable what="Security profiles" /></div>
  const list = pr.data || []
  return (
    <div className="bx-page">
      <LoadError error={pr.error} />
      <section className="sbx-sec">
        <div className="sbx-sec-head"><h3>Profiles</h3>
          <span className="sec-count">{list.length}</span>
          <div className="sec-actions">
            <Button variant="ghost" onClick={() => setEditing(blankProfile())}>+ New profile</Button>
          </div>
        </div>
        {!pr.data && !pr.error && <div className="dim">…</div>}
        {pr.data && list.length === 0 && <EmptyState>no profiles</EmptyState>}
        <ul className="staged-list rev-list bx-profiles">
          {list.map((p) => (
            <li key={p.id} className={editing?.id === p.id ? 'active' : ''}>
              <span className="bx-prof-main grow">
                <span><b>{p.name}</b> {p.builtin && <Tag>built-in</Tag>}</span>
                <span className="dim small">
                  {p.network_off ? 'network off' : `default ${p.default_verdict}`}
                  {' · '}{(p.allow_hosts || []).length} allowed · {(p.deny_hosts || []).length} denied
                  {' · '}{(p.secrets || []).length} secret(s)
                  {p.auto_handle ? ' · auto-handle' : ''}
                  {' · '}{p.separate_box ? `own ${p.box_runtime} box (${p.box_image}, ${p.box_mem_mb || '?'} MB)` : 'shared box'}
                  {' · services '}{String(p.service_placement || '?').replace('_', ' ')}
                </span>
                <span className="small">projects: <ProjectList slugs={p.projects} empty="none" /></span>
              </span>
              <Button variant="ghost" onClick={() => setEditing({ ...p })}>
                {p.builtin ? 'View' : 'Edit'}</Button>
              <Button variant="ghost" onClick={() => setEditing({
                // placement and runtime are picked again: a copy is a new
                // profile, and new profiles have no default for either
                ...p, id: null, builtin: false, name: `${p.name} copy`, projects: [],
                service_placement: '', box_runtime: '' })}>Duplicate</Button>
              {!p.builtin && <Button variant="ghost" danger onClick={() => remove(p)}>Delete</Button>}
            </li>
          ))}
        </ul>
      </section>

      {editing && (
        <ProfileForm key={editing.id ?? 'new'} initial={editing} secrets={secrets} variants={variants}
                     names={list.filter((p) => p.id !== editing.id).map((p) => p.name)}
                     onCancel={() => setEditing(null)}
                     onSaved={() => { setEditing(null); pr.reload() }} />
      )}

      <Assignments profiles={list} projects={projects} onDone={pr.reload} />
    </div>
  )
}

function ProfileForm({ initial, secrets, variants, names, onCancel, onSaved }) {
  const [p, setP] = useState(initial)
  const [allowText, setAllowText] = useState((initial.allow_hosts || []).join('\n'))
  const [denyText, setDenyText] = useState((initial.deny_hosts || []).join('\n'))
  const [busy, setBusy] = useState(false)
  const [tried, setTried] = useState(false)
  const ro = !!initial.builtin
  const isNew = initial.id == null
  const set = (k, v) => setP((x) => ({ ...x, [k]: v }))
  const full = { ...p, allow_hosts: parseHosts(allowText), deny_hosts: parseHosts(denyText) }
  const errs = validateProfile(full, { names })
  const ok = Object.keys(errs).length === 0
  const show = (k) => (tried || k === 'hosts' ? errs[k] : null)
  // secret names known to the server, plus any the profile names that were
  // since deleted (so they can be seen and unticked)
  const secretList = [...new Set([...secrets, ...(p.secrets || [])])].sort()

  async function save(e) {
    e.preventDefault()
    setTried(true)
    if (!ok || ro) return
    setBusy(true)
    try {
      const body = profilePayload(full)
      if (isNew) await createProfile(body)
      else await updateProfile(p.id, body)
      notify(`profile ${body.name} saved`)
      onSaved()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  const radio = (name, value, cur, onPick) => (
    <input type="radio" name={name} value={value} checked={cur === value}
           disabled={ro} onChange={() => onPick(value)} />
  )

  return (
    <form className="sbx-card bx-form bx-prof-form" onSubmit={save}>
      <div className="sbx-sec-head">
        <h3>{ro ? `${p.name} (built-in, read-only)` : isNew ? 'New profile' : `Edit ${initial.name}`}</h3>
      </div>
      <fieldset disabled={ro} className="bx-fs">
        <Input label="Name" value={p.name} error={show('name')} onChange={(e) => set('name', e.target.value)} />

        <div className="field">
          <span>Secrets it can have <span className="dim small">(names only — values never leave the host)</span></span>
          {secretList.length === 0 && <span className="dim small">no secrets stored</span>}
          <div className="bx-checks">
            {secretList.map((s) => (
              <label key={s} className="check-row">
                <input type="checkbox" checked={(p.secrets || []).includes(s)}
                       onChange={(e) => set('secrets', e.target.checked
                         ? [...(p.secrets || []), s] : (p.secrets || []).filter((x) => x !== s))} />
                <span className="mono small">{s}</span>
              </label>
            ))}
          </div>
        </div>

        <div className="field">
          <span>Network</span>
          <label className="check-row">
            <input type="checkbox" checked={!!p.network_off} onChange={(e) => set('network_off', e.target.checked)} />
            <span>Network off — no egress at all, whatever the lists say</span>
          </label>
          <div className="bx-radios">
            <span className="small">A host on neither list is</span>
            <label className="check-row">{radio('verdict', 'deny', p.default_verdict, (v) => set('default_verdict', v))}
              <span>denied (asks you)</span></label>
            <label className="check-row">{radio('verdict', 'allow', p.default_verdict, (v) => set('default_verdict', v))}
              <span>allowed</span></label>
          </div>
        </div>
        <div className="bx-two">
          <Input textarea label="Allow list (one host per line)" rows={4} spellCheck={false}
                 value={allowText} onChange={(e) => setAllowText(e.target.value)} />
          <Input textarea label="Deny list — beats every allow" rows={4} spellCheck={false}
                 value={denyText} onChange={(e) => setDenyText(e.target.value)} />
        </div>
        {show('hosts') && <span className="error small">{errs.hosts}</span>}

        <div className="field">
          <label className="check-row">
            <input type="checkbox" checked={!!p.auto_handle} onChange={(e) => set('auto_handle', e.target.checked)} />
            <span>Auto-handle all alerts for its projects</span>
          </label>
          <span className="field-hint">{NEVER_AUTO}</span>
          <label className="check-row">
            <input type="checkbox" checked={!!p.allow_services} onChange={(e) => set('allow_services', e.target.checked)} />
            <span>The agent may file service requests</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={!!p.allow_package_requests}
                   onChange={(e) => set('allow_package_requests', e.target.checked)} />
            <span>The agent may file package requests</span>
          </label>
        </div>

        <div className="bx-boxsec">
          <div className="small"><b>Box</b></div>
          <label className="check-row">
            <input type="checkbox" checked={!!p.separate_box} onChange={(e) => set('separate_box', e.target.checked)} />
            <span>Separate box — each project gets its own box instead of the shared one</span>
          </label>
          <div className="field">
            <span>Box runtime <span className="dim small">(required)</span></span>
            <div className="bx-radios">
              {RUNTIMES.map((o) => (
                <label key={o.value} className="check-row">
                  {radio('runtime', o.value, p.box_runtime, (v) => set('box_runtime', v))}
                  <span>{o.label} <span className="dim small">— {o.hint}</span></span>
                </label>
              ))}
            </div>
            {show('box_runtime') && <span className="error small">{errs.box_runtime}</span>}
          </div>
          <div className="field">
            <span>Service placement <span className="dim small">(required)</span></span>
            <div className="bx-radios col">
              {PLACEMENTS.map((o) => (
                <label key={o.value} className="check-row">
                  {radio('placement', o.value, p.service_placement, (v) => set('service_placement', v))}
                  <span>{o.label} <span className="dim small">— {o.hint}</span></span>
                </label>
              ))}
            </div>
            {show('service_placement') && <span className="error small">{errs.service_placement}</span>}
          </div>
          <div className="row">
            <Select label="Box image variant" value={p.box_image || 'main'} disabled={ro}
                    onChange={(e) => set('box_image', e.target.value)}
                    options={[...new Set([...variants, p.box_image || 'main'])]} />
            <Input label="Box memory (MB)" type="number" min={256} step={64} value={p.box_mem_mb ?? ''}
                   error={show('box_mem_mb')} onChange={(e) => set('box_mem_mb', e.target.value)} />
          </div>
          <span className="field-hint">Host is a 4 GB Pi: the guest budget is about 2.2 GB for every
            box together. <code>desktop</code> needs at least 1280 MB.</span>
        </div>
      </fieldset>
      <div className="row">
        {tried && !ok && <span className="error small">fix the fields marked above</span>}
        <span className="grow" />
        <Button variant="ghost" onClick={onCancel}>{ro ? 'Close' : 'Cancel'}</Button>
        {!ro && <Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save profile'}</Button>}
      </div>
    </form>
  )
}

function Assignments({ profiles, projects, onDone }) {
  const ask = useAsk()
  const of = useMemo(() => {
    const m = {}
    for (const p of profiles) for (const s of p.projects || []) m[s] = p
    return m
  }, [profiles])
  const def = profiles.find((p) => p.builtin && /^default$/i.test(p.name))
  if (!projects.length || !profiles.length) return null
  async function change(slug, id) {
    const to = profiles.find((p) => String(p.id) === String(id))
    if (!to) return
    if (!await ask.confirm(`Move ${slug} to ${to.name}?`, {
      body: `From its next turn: secrets ${(to.secrets || []).join(', ') || 'none'}; `
        + `${to.network_off ? 'no network' : `unlisted hosts ${to.default_verdict === 'allow' ? 'ALLOWED' : 'denied'}`}; `
        + `${to.separate_box ? `its own ${to.box_runtime} box` : 'the shared box'}.`,
      confirmLabel: 'Move', danger: to.default_verdict === 'allow' })) return
    try { await assignProfile(slug, to.id); onDone() } catch (e) { notifyError(e) }
  }
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Projects</h3></div>
      <ul className="staged-list rev-list">
        {projects.map((pj) => {
          const cur = of[pj.slug] || def
          return (
            <li key={pj.slug}>
              <span className="grow">{pj.name} <span className="dim small mono">{pj.slug}</span></span>
              <Select aria-label={`profile for ${pj.slug}`} value={cur ? String(cur.id) : ''}
                      onChange={(e) => change(pj.slug, e.target.value)}
                      options={profiles.map((p) => ({ value: String(p.id), label: p.name }))} />
            </li>
          )
        })}
      </ul>
    </section>
  )
}
