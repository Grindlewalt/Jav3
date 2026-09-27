import { useEffect, useMemo, useState } from 'react'
import { Button, EmptyState, Input, Select, Tag } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import {
  assignProfile, createProfile, deleteProfile, listProfiles, makeDefaultProfile, secretNames,
  updateProfile,
} from '../boxes/api/profiles.js'
import { listImages } from '../boxes/api/images.js'
import { listBoxes } from '../boxes/api/vms.js'
import { listProjects } from '../boxes/api/persist.js'
import {
  assignableProjects, blankProfile, deleteBlock, NETWORK_MODES, networkMode, newSitesText, parseHosts, PLACEMENTS,
  profilePayload, RUNS_IN, runsIn, runsInText, validateProfile, withNetworkMode, withRunsIn,
} from '../boxes/logic.js'
import { LoadError, ProjectList, RuntimeStatus, Unavailable, useLoad } from '../boxes/ui.jsx'

// Security > Profiles: a project's whole security posture in one named row —
// which secrets it may have, how its egress is judged, whether its alerts are
// auto-handled, and the box it runs in. Every profile can be renamed, edited
// and deleted, except the default (the one new and unassigned projects use;
// "Make default" moves the mark) and one a project still uses. Every project
// has exactly one profile. First-run setup creates the first default.
//
// A new profile starts on the shared box with per-project service boxes;
// every choice is one radio (Network, Runs in) over the stored fields.

const NEVER_AUTO = 'service_* and svc_unreported, '
  + 'package_* and image_variant_built, unexpected_process and proc_report_mismatch, '
  + 'profile_changed and profiles_migrated, docker_weak_isolation / docker_hardening_refused / '
  + 'docker_socket_refused, persist_imported and persist_disk_deleted, plus egress_anomaly, '
  + 'host_cut and secret_leak.'

export default function Profiles() {
  const pr = useLoad(listProfiles)
  const [editing, setEditing] = useState(null)       // a profile object (id null = new)
  const [secrets, setSecrets] = useState([])
  const [variants, setVariants] = useState(['main', 'dev', 'desktop'])
  const [projects, setProjects] = useState([])
  const [runtimes, setRuntimes] = useState(null)
  const [budget, setBudget] = useState(null)
  const ask = useAsk()

  useEffect(() => {
    secretNames().then(setSecrets).catch(() => {})
    listImages().then((r) => { if (r.variants.length) setVariants(r.variants.map((v) => v.name)) })
      .catch(() => {})
    listProjects().then((ps) => setProjects(assignableProjects(ps))).catch(() => {})
    listBoxes().then((r) => { setRuntimes(r.runtimes); setBudget(r.budget || null) }).catch(() => {})
  }, [])

  // the default cannot be deleted (409), nor can a profile a project still
  // uses (409 "in use by"): deleteBlock says why before the server has to
  async function remove(p) {
    if (!await ask.confirm(`Delete profile ${p.name}?`, {
      body: 'No project uses it. This cannot be undone.',
      confirmLabel: 'Delete', danger: true })) return
    try { await deleteProfile(p.id); pr.reload() } catch (e) { notifyError(e) }
  }

  async function makeDefault(p) {
    if (!await ask.confirm(`Make ${p.name} the default?`, {
      body: 'New projects, and every project without a profile of its own, will use it '
        + `from their next turn: new sites ${newSitesText(p)}; runs in ${runsInText(p)}.`,
      confirmLabel: 'Make default', danger: p.default_verdict === 'allow' && !p.network_off })) return
    try { await makeDefaultProfile(p.id); notify(`${p.name} is now the default`); pr.reload() }
    catch (e) { notifyError(e) }
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
        {pr.data && list.length === 0 && <EmptyState>no profiles yet: the first project creates the default</EmptyState>}
        <ul className="staged-list rev-list bx-profiles">
          {list.map((p) => (
            <li key={p.id} className={editing?.id === p.id ? 'active' : ''}>
              <span className="bx-prof-main grow">
                <span><b>{p.name}</b> {p.is_default && <Tag>default</Tag>}</span>
                <span className="small">
                  New sites: {newSitesText(p)}
                  {' · '}{(p.allow_hosts || []).length} always allowed
                  {' · '}{(p.deny_hosts || []).length} blocked
                </span>
                <span className="small">
                  Secrets: {(p.secrets || []).length ? p.secrets.join(', ') : 'none'}
                  {' · '}Runs in: {runsInText(p)}
                  {p.auto_handle ? ' · auto-handles alerts' : ''}
                </span>
                <span className="dim small">used by: <ProjectList slugs={p.projects} empty="no project" /></span>
                {deleteBlock(p) && <span className="dim small">cannot delete: {deleteBlock(p)}</span>}
              </span>
              <Button variant="ghost" onClick={() => setEditing({ ...p })}>Edit</Button>
              <Button variant="ghost" onClick={() => setEditing({
                ...p, id: null, is_default: false, name: `${p.name} copy`, projects: [] })}>
                Duplicate</Button>
              {!p.is_default && (
                <Button variant="ghost" onClick={() => makeDefault(p)}>Make default</Button>)}
              <Button variant="ghost" danger disabled={!!deleteBlock(p)}
                      title={deleteBlock(p) || undefined}
                      onClick={() => remove(p)}>Delete</Button>
            </li>
          ))}
        </ul>
      </section>

      {editing && (
        <ProfileForm key={editing.id ?? 'new'} initial={editing} secrets={secrets} variants={variants}
                     runtimes={runtimes} budget={budget}
                     names={list.filter((p) => p.id !== editing.id).map((p) => p.name)}
                     onCancel={() => setEditing(null)}
                     onSaved={() => { setEditing(null); pr.reload() }} />
      )}

      <Assignments profiles={list} projects={projects} onDone={pr.reload} />
    </div>
  )
}

function ProfileForm({ initial, secrets, variants, runtimes, budget, names, onCancel, onSaved }) {
  const [p, setP] = useState({
    ...initial,
    // an old row may carry neither: the same defaults a new profile gets
    service_placement: initial.service_placement || 'per_project',
    box_runtime: initial.box_runtime || 'kvm',
  })
  const [allowText, setAllowText] = useState((initial.allow_hosts || []).join('\n'))
  const [denyText, setDenyText] = useState((initial.deny_hosts || []).join('\n'))
  const [busy, setBusy] = useState(false)
  const [tried, setTried] = useState(false)
  const isNew = initial.id == null
  const set = (k, v) => setP((x) => ({ ...x, [k]: v }))
  const patch = (o) => setP((x) => ({ ...x, ...o }))
  const full = { ...p, allow_hosts: parseHosts(allowText), deny_hosts: parseHosts(denyText) }
  const errs = validateProfile(full, { names })
  const ok = Object.keys(errs).length === 0
  const show = (k) => (tried || k === 'hosts' ? errs[k] : null)
  // secret names known to the server, plus any the profile names that were
  // since deleted (so they can be seen and unticked)
  const secretList = [...new Set([...secrets, ...(p.secrets || [])])].sort()
  const net = networkMode(p)
  const where = runsIn(p)

  async function save(e) {
    e.preventDefault()
    setTried(true)
    if (!ok) return
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

  const radio = (name, value, cur, onPick, off = false) => (
    <input type="radio" name={name} value={value} checked={cur === value}
           disabled={off} onChange={() => onPick(value)} />
  )
  // why a "Runs in" choice cannot be picked on this server (null = it can)
  const unavailable = (v) => {
    if (!runtimes || v === 'shared') return null
    const rt = v === 'container' ? runtimes.docker : runtimes.kvm
    return rt && !rt.available ? (rt.reason || 'not available on this server') : null
  }

  return (
    <form className="sbx-card bx-form bx-prof-form" onSubmit={save}>
      <div className="sbx-sec-head">
        <h3>{isNew ? 'New profile' : `Edit ${initial.name}`}{initial.is_default ? ' (the default)' : ''}</h3>
      </div>
      <fieldset className="bx-fs">
        <Input label="Name" value={p.name} error={show('name')}
               onChange={(e) => set('name', e.target.value)} />

        <div className="field">
          <span>Network</span>
          <div className="bx-radios">
            {NETWORK_MODES.map((o) => (
              <label key={o.value} className="check-row" title={o.hint}>
                {radio('network', o.value, net, (v) => patch(withNetworkMode(v)))}
                <span>{o.label}</span>
              </label>
            ))}
          </div>
          <span className="field-hint">{NETWORK_MODES.find((o) => o.value === net)?.hint}</span>
        </div>
        <div className="bx-two">
          <Input textarea label="Always allow (one site per line)" rows={4} spellCheck={false}
                 value={allowText} onChange={(e) => setAllowText(e.target.value)} />
          <Input textarea label="Always block (wins)" rows={4} spellCheck={false}
                 value={denyText} onChange={(e) => setDenyText(e.target.value)} />
        </div>
        {show('hosts') && <span className="error small">{errs.hosts}</span>}

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
          <span>Runs in</span>
          <div className="bx-radios">
            {RUNS_IN.map((o) => {
              const why = unavailable(o.value)
              return (
                <label key={o.value} className={`check-row${why ? ' bx-runtime-off' : ''}`}
                       title={why ? `unavailable: ${why}` : o.hint}>
                  {radio('runsin', o.value, where, (v) => patch(withRunsIn(v)), !!why && where !== o.value)}
                  <span>{o.label}</span>
                </label>
              )
            })}
          </div>
          <span className="field-hint">{RUNS_IN.find((o) => o.value === where)?.hint}
            {RUNS_IN.filter((o) => unavailable(o.value)).map((o) => (
              <span key={o.value} className="dim"> · {o.label} unavailable: {unavailable(o.value)}</span>))}
          </span>
          {where === 'container' && runtimes && <RuntimeStatus runtimes={runtimes} compact />}
          {show('box_runtime') && <span className="error small">{errs.box_runtime}</span>}
        </div>
        {where !== 'shared' && (
          <>
            <div className="row">
              <Select label="Image" value={p.box_image || 'main'}
                      onChange={(e) => set('box_image', e.target.value)}
                      options={[...new Set([...variants, p.box_image || 'main'])]} />
              <Input label="Memory (MB)" type="number" min={256} step={64} value={p.box_mem_mb ?? ''}
                     error={show('box_mem_mb')} onChange={(e) => set('box_mem_mb', e.target.value)} />
            </div>
            {budget?.ram_mb_cap && (
              <span className="field-hint">Every box together may use about {budget.ram_mb_cap} MB
                {budget.host_ram_mb ? ` (this host has ${budget.host_ram_mb} MB)` : ''}.
                {' '}<code>desktop</code> needs at least 1280 MB.</span>)}
          </>
        )}

        <div className="field">
          <label className="check-row">
            <input type="checkbox" checked={!!p.allow_services} onChange={(e) => set('allow_services', e.target.checked)} />
            <span>Agent may request services</span>
          </label>
          {p.allow_services && (
            <div className="bx-radios col bx-indent">
              <span className="small">each service runs in</span>
              {PLACEMENTS.map((o) => (
                <label key={o.value} className="check-row">
                  {radio('placement', o.value, p.service_placement, (v) => set('service_placement', v))}
                  <span>{o.label} <span className="dim small">— {o.hint}</span></span>
                </label>
              ))}
            </div>
          )}
          {show('service_placement') && <span className="error small">{errs.service_placement}</span>}
          <label className="check-row">
            <input type="checkbox" checked={!!p.allow_package_requests}
                   onChange={(e) => set('allow_package_requests', e.target.checked)} />
            <span>Agent may request packages</span>
          </label>
          <label className="check-row">
            <input type="checkbox" checked={!!p.auto_handle} onChange={(e) => set('auto_handle', e.target.checked)} />
            <span>Auto-handle alerts</span>
          </label>
          <details className="field-hint">
            <summary>which alerts are never auto-handled</summary>
            {NEVER_AUTO}
          </details>
        </div>
      </fieldset>
      <div className="row">
        {tried && !ok && <span className="error small">fix the fields marked above</span>}
        <span className="grow" />
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save profile'}</Button>
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
  const def = profiles.find((p) => p.is_default)
  if (!projects.length || !profiles.length) return null
  async function change(slug, id) {
    const to = profiles.find((p) => String(p.id) === String(id))
    if (!to) return
    if (!await ask.confirm(`Move ${slug} to ${to.name}?`, {
      body: `From its next turn: secrets ${(to.secrets || []).join(', ') || 'none'}; `
        + `new sites: ${to.default_verdict === 'allow' && !to.network_off ? 'ALLOWED' : newSitesText(to)}; `
        + `runs in: ${runsInText(to)}.`,
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
