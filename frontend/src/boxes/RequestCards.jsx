// The two new kinds of request the operator decides on: a package for an
// image variant, and a service (a long-lived process in a service box).
// Used by the Security queue and by the VM manager's Catalogue.
//
// Everything in a request — package names, commands, reasons, argv, env,
// diffs — was written by the agent. All of it renders as text nodes.
import { Fragment, useEffect, useState } from 'react'
import { Button, Modal, Select, Tag } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { ago } from '../format.js'
import { approvePackage, listPackages, rejectPackage, resolvePackages } from './api/packages.js'
import { approveService, getService, rejectService } from './api/services.js'
import { approveEnvironment, rejectEnvironment } from './api/environments.js'
import { approveSkillRequest, fetchSkillRequest, rejectSkillRequest } from './api/skillRequests.js'
import {
  diffLines, EXPOSE_LABEL, exposeChoices, exposeDefault, exposePayload, packageReach, PLACEMENTS,
  packagesByManager, skillCardState,
} from './logic.js'
import { ProjectList } from './ui.jsx'

// ---- packages ------------------------------------------------------------------

export function PackageSummary({ p }) {
  return (
    <div className="bx-pkg">
      <div className="bx-pkg-top">
        <Tag>{p.manager}</Tag>
        <b className="mono">{p.package}</b>
        {p.version_req && <span className="mono small dim">{p.version_req}</span>}
        {p.resolved_version && <span className="small">→ <span className="mono">{p.resolved_version}</span></span>}
        {p.project_slug ? <Tag>{p.project_slug}</Tag> : <Tag>{p.source || 'operator'}</Tag>}
        <span className="dim small">{ago(p.created_at)}</span>
      </div>
      <dl className="bx-kv small">
        {p.requested_command && <><dt>asked to run</dt><dd className="mono bx-strike">{p.requested_command}</dd></>}
        <dt>will run</dt><dd className="mono">{p.canonical_command || '(pinned once resolved)'}</dd>
        {p.integrity && p.resolved_version && (
          <><dt>integrity</dt><dd className="mono bx-hash">{p.integrity}</dd></>)}
        {p.reason && <><dt>reason</dt><dd>{p.reason}</dd></>}
        {p.conversation_id && <><dt>conversation</dt><dd className="mono">{p.conversation_id}</dd></>}
      </dl>
    </div>
  )
}

// The approval card: the variant it installs into, and every project that
// variant reaches (operator decision 0.2) — directly, or through a variant
// built from it. For the row's own target the server's `card` sentence and
// `variant_used_by_detail` are shown as sent.
// Approval needs the dry-run's pinned version and integrity. That dry-run runs
// in the background when the request is filed; when it has not succeeded the
// modal says why and resolves on the spot, instead of an Approve that can only
// answer "not resolved yet" (2026-10-02: a request stuck there for a day).
export function PackageApprove({ p: given, variants, onClose, onDone }) {
  const [p, setP] = useState(given)
  const [target, setTarget] = useState(given?.target_variant || 'main')
  const [ack, setAck] = useState(false)
  const [build, setBuild] = useState(false)
  const [busy, setBusy] = useState(false)
  const [resolving, setResolving] = useState(false)
  useEffect(() => {
    setP(given); setTarget(given?.target_variant || 'main'); setAck(false); setBuild(false)
  }, [given])
  if (!p) return null
  const reach = packageReach(p, target, { variants })
  const names = [...new Set([...(variants || []).map((x) => x.name), p.target_variant].filter(Boolean))]
  const unresolved = !p.resolved_version
  // integrity carries "unresolved: <why>" when a dry-run failed
  const why = unresolved && /^unresolved: /.test(p.integrity || '')
    ? p.integrity.replace(/^unresolved: /, '') : null
  async function resolve() {
    setResolving(true)
    try {
      const r = await resolvePackages()
      const fresh = (await listPackages()).find((x) => x.id === p.id)
      if (fresh) setP(fresh)
      if (r?.error) notifyError(new Error(`resolve: ${r.error}`))
    } catch (e) { notifyError(e) }
    setResolving(false)
  }
  async function go() {
    setBusy(true)
    try {
      const r = await approvePackage(p.id, { targetVariant: target, build })
      if (build) notify(r?.build_started ? `building ${target}` : `approved — the build of ${target} did not start (busy?)`)
      onDone()
    } catch (e) { notifyError(e) }
    setBusy(false)
  }
  return (
    <Modal open title={`Approve ${p.package}?`} onClose={onClose} onSubmit={go} width={560}
           footer={<>
             <Button variant="ghost" onClick={onClose}>Cancel</Button>
             <Button type="submit" disabled={!ack || busy || unresolved}>Approve</Button>
           </>}>
      <PackageSummary p={p} />
      {unresolved && (
        <div className="bx-unresolved">
          <p className="small">
            <b>Not resolved yet.</b> Approval needs the exact version and its integrity,
            which a dry-run in a builder box pins
            {why ? <>. The last dry-run failed: <span className="mono">{why}</span></> : '. It has not run, or did not finish.'}
          </p>
          <Button variant="ghost" disabled={resolving} onClick={resolve}>
            {resolving ? 'Resolving… (about a minute)' : 'Resolve now'}</Button>
        </div>
      )}
      <div className="row">
        <Select label="Installs into" value={target} onChange={(e) => setTarget(e.target.value)}
                options={names.length ? names : [target]} />
      </div>
      {target === p.target_variant && p.card
        ? <p className="bx-reach">{p.card}</p>
        : <p className="bx-reach">installs into <code>{target}</code> — used by:{' '}
            <ProjectList slugs={reach.all} empty="no project yet" /></p>}
      {reach.via.length > 0 && (
        <p className="dim small">
          {reach.via.map(([v, slugs]) => (
            <span key={v}>via <code>{v}</code>: <ProjectList slugs={slugs} />{' '}</span>))}
        </p>)}
      <p className="dim small">Every project on <code>{target}</code> gets it at the variant's next
        version. The host runs the "will run" command, never the agent's.</p>
      <label className="check-row">
        <input type="checkbox" checked={build} onChange={(e) => setBuild(e.target.checked)} />
        <span>Build a new version of <code>{target}</code> now</span>
      </label>
      <label className="check-row">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>I have checked the package name, version and integrity</span>
      </label>
    </Modal>
  )
}

export function usePackageReject(onDone) {
  const ask = useAsk()
  return async (p) => {
    const reason = await ask.prompt(`Reject ${p.package}? Reason (optional)`, '', { confirmLabel: 'Reject' })
    if (reason === null) return
    try { await rejectPackage(p.id, reason); onDone() } catch (e) { notifyError(e) }
  }
}

// ---- services ------------------------------------------------------------------

// The service request, in full: the definition, what changed since the
// approved version, the ports and where each is exposed, and the REQUIRED
// placement choice (preselected from the project's profile, changeable here,
// stored on the service row). `lan` is {ip, configured, error} from the
// list response.
export function ServiceRequest({ s, lan, profilePlacement, onDone }) {
  const lanIp = lan?.ip || ''
  const [full, setFull] = useState(null)
  const [placement, setPlacement] = useState(s.placement || profilePlacement || '')
  const [expose, setExpose] = useState(() => Object.fromEntries((s.ports || []).map((p) =>
    [p.port, exposeDefault(p.expose, lanIp)])))
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const ask = useAsk()
  useEffect(() => {
    getService(s.id).then(setFull).catch(() => setFull({ diff: null }))
  }, [s.id])

  async function approve() {
    setBusy(true)
    try {
      await approveService(s.id, { placement, exposePorts: exposePayload(expose, s.ports, lanIp) })
      onDone()
    } catch (e) { notifyError(e) }
    setBusy(false)
  }
  async function reject() {
    const reason = await ask.prompt(`Reject service ${s.name}? Reason (optional)`, '',
                                    { confirmLabel: 'Reject' })
    if (reason === null) return
    try { await rejectService(s.id, reason); onDone() } catch (e) { notifyError(e) }
  }

  const env = Object.entries(s.env || {})
  const diff = diffLines(full?.diff)
  return (
    <div className="sbx-card bx-svc-req">
      <div className="bx-pkg-top">
        <Tag tone="pending">service</Tag>
        <b className="mono">{s.name}</b>
        {s.project_slug && <Tag>{s.project_slug}</Tag>}
        {s.supersedes_id && <Tag title="replaces an approved version">update of #{s.supersedes_id}</Tag>}
        <span className="dim small">{ago(s.created_at)}</span>
      </div>
      {s.description && <div className="small">{s.description}</div>}
      <dl className="bx-kv small">
        <dt>command</dt><dd className="mono">{(s.command || []).map((a) => JSON.stringify(a)).join(' ')}</dd>
        <dt>workdir</dt><dd className="mono">{s.workdir || '.'}</dd>
        <dt>files</dt><dd className="mono">{(s.files || []).join('  ') || '–'}</dd>
        <dt>restart</dt><dd>{s.restart}</dd>
        <dt>egress</dt><dd className="mono">{(s.egress_hosts || []).join('  ') || 'none (deny all)'}</dd>
        {env.length > 0 && <><dt>env</dt>
          <dd className="mono">{env.map(([k, v]) => `${k}=${v}`).join('  ')}</dd></>}
        <dt>content</dt><dd className="mono bx-hash" title="sha256 of the snapshotted files">
          {s.artifact_sha256}</dd>
        {s.reason && <><dt>reason</dt><dd>{s.reason}</dd></>}
      </dl>

      {s.supersedes_id && (
        <details className="bx-diff" open>
          <summary className="small">changes since approved #{s.supersedes_id}</summary>
          {!full && <div className="dim small">…</div>}
          {full && !diff.length && <div className="dim small">no file changes (definition only)</div>}
          {diff.length > 0 && (
            <pre className="mono small">{diff.map((l, i) => (
              <span key={i} className={`bx-d-${l.cls}`}>{l.text}{'\n'}</span>))}</pre>
          )}
        </details>
      )}

      {(s.ports || []).length > 0 && (
        <div className="bx-ports">
          <div className="small"><b>Ports</b> <span className="dim">— nothing is reachable unless you expose it</span></div>
          {s.ports.map((p) => (
            <div className="row bx-port" key={p.port}>
              <span className="mono">{p.port}/{p.protocol || 'tcp'}</span>
              <span className="dim small grow">{p.purpose}{p.expose && p.expose !== 'none'
                ? ` · asked for ${p.expose}` : ''}</span>
              <Select aria-label={`expose port ${p.port}`} value={expose[p.port] || 'none'}
                      onChange={(e) => setExpose((x) => ({ ...x, [p.port]: e.target.value }))}
                      options={exposeChoices(p.expose, lanIp)
                        .map((v) => ({ value: v, label: EXPOSE_LABEL[v] }))} />
            </div>
          ))}
          {!lanIp && (s.ports || []).some((p) => p.expose === 'lan') && (
            <div className="dim small">LAN exposure is unavailable: {lan?.configured
              ? <>the configured address <code>{lan.configured}</code> was refused
                  {lan.error ? <> ({lan.error})</> : null}.</>
              : <>no services LAN address is configured (<code>services_lan_ip</code>).</>}</div>)}
          {lanIp && Object.values(expose).includes('lan') && (
            <div className="warn small">LAN ports bind on {lanIp} only — never on Jav3's own address.</div>)}
        </div>
      )}

      <fieldset className="bx-placement">
        <legend className="small"><b>Placement</b> <span className="dim">(required
          {profilePlacement ? ` — the profile says ${profilePlacement.replace('_', ' ')}` : ''})</span></legend>
        {PLACEMENTS.map((o) => (
          <label key={o.value} className="check-row">
            <input type="radio" name={`place-${s.id}`} value={o.value}
                   checked={placement === o.value} onChange={() => setPlacement(o.value)} />
            <span>{o.label} <span className="dim small">— {o.hint}</span></span>
          </label>
        ))}
      </fieldset>

      <label className="check-row">
        <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
        <span>I have read the definition{s.supersedes_id ? ' and the changes' : ''}; run exactly this
          (content {String(s.artifact_sha256 || '').slice(0, 12)})</span>
      </label>
      <div className="row">
        <span className="grow" />
        <Button variant="ghost" danger onClick={reject}>Reject</Button>
        <Button disabled={!ack || !placement || busy} onClick={approve}>Approve service</Button>
      </div>
    </div>
  )
}

// ---- environment requests ---------------------------------------------------------

// A new image variant plus the packages it adds, filed by an agent for its project.
// Approve builds the image and moves the project onto it; nothing else changes.
export function EnvironmentCard({ e, onDone }) {
  const [busy, setBusy] = useState(false)
  const [resolving, setResolving] = useState(false)
  const ask = useAsk()
  const pending = e.status === 'pending'
  async function resolve() {
    setResolving(true)
    try {
      const r = await resolvePackages()
      if (r?.error) notifyError(new Error(`resolve: ${r.error}`))
      onDone()
    } catch (err) { notifyError(err) }
    setResolving(false)
  }
  async function approve() {
    const ok = await ask.confirm(
      `Approve environment ${e.name}? The host builds the image from ${e.from_variant} with the `
      + `pinned packages shown, then ${e.project_slug} boots on it.`, { confirmLabel: 'Approve & build' })
    if (!ok) return
    setBusy(true)
    try {
      const r = await approveEnvironment(e.id, { build: true })
      notify(r?.build_started ? `building ${e.name}` : `approved — the build of ${e.name} did not start (busy?)`)
      onDone()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }
  async function reject() {
    const reason = await ask.prompt(`Reject environment ${e.name}? Reason (optional)`, '',
                                    { confirmLabel: 'Reject' })
    if (reason === null) return
    try { await rejectEnvironment(e.id, reason); onDone() } catch (err) { notifyError(err) }
  }
  return (
    <div className="sbx-card bx-svc-req">
      <div className="bx-pkg-top">
        <Tag tone="pending">environment</Tag>
        <b className="mono">{e.name}</b>
        {e.project_slug && <Tag>{e.project_slug}</Tag>}
        <Tag>agent</Tag>
        <span className="dim small">{ago(e.created_at)}</span>
      </div>
      <dl className="bx-kv small">
        <dt>new image</dt><dd><span className="mono">{e.name}</span> (from <span className="mono">{e.from_variant}</span>)</dd>
        {packagesByManager(e.packages).map(([m, list]) => (
          <Fragment key={m}>
            <dt>adds {m}</dt>
            <dd>{list.map((p) => (
              <div key={p.id ?? p.package} className="bx-pkg">
                <b className="mono">{p.package}</b>
                {p.version_req && <span className="mono dim"> {p.version_req}</span>}
                {p.resolved_version
                  ? <span> → <span className="mono">{p.resolved_version}</span></span>
                  : <span className="warn"> not resolved yet</span>}
                {p.integrity && p.resolved_version && (
                  <div className="mono dim bx-hash">{p.integrity}</div>)}
              </div>))}</dd>
          </Fragment>
        ))}
        <dt>memory floor</dt>
        <dd>{e.min_mem_mb ? `${e.min_mem_mb} MB` : <span className="dim">as {e.from_variant || 'main'}</span>}</dd>
        {e.reason && <><dt>why</dt><dd>{e.reason}</dd></>}
        {e.conversation_id && <><dt>conversation</dt><dd className="mono">{e.conversation_id}</dd></>}
        {pending && e.plan && <><dt>on approve</dt><dd>{e.plan}</dd></>}
        {pending && <><dt>other projects</dt><dd>unchanged</dd></>}
        {!pending && <><dt>status</dt><dd>{e.status}{e.decided_by ? ` by ${e.decided_by}` : ''}</dd></>}
        {e.profile && <><dt>profile</dt><dd>{e.project_slug} now uses profile <span className="mono">{e.profile.name}</span></dd></>}
        {e.build && <><dt>build</dt><dd>{e.build.version} · {e.build.status}
          {e.build.error ? <span className="warn"> {e.build.error}</span> : null}</dd></>}
      </dl>
      {pending && !e.resolved && (
        <div className="bx-unresolved">
          <p className="small"><b>Not resolved yet.</b> Approval needs every package's exact version and
            integrity, which a dry-run in a builder box pins.</p>
          <Button variant="ghost" disabled={resolving} onClick={resolve}>
            {resolving ? 'Resolving… (about a minute)' : 'Resolve now'}</Button>
        </div>
      )}
      {pending && (
        <div className="row">
          <span className="grow" />
          <Button variant="ghost" danger onClick={reject}>Reject</Button>
          <Button disabled={!e.resolved || busy} onClick={approve}>Approve &amp; build</Button>
        </div>
      )}
    </div>
  )
}

// ---- skill install requests -------------------------------------------------------

// The source, reason and every fetched word are agent- or internet-written.
// Pending: only the source is shown and nothing has been contacted. Fetch makes the
// host download and vet it; Install & grant sends the hash of the text read here.
export function SkillRequestCard({ r, onDone }) {
  const [busy, setBusy] = useState(false)
  const ask = useAsk()
  const st = skillCardState(r)
  async function fetchIt() {
    setBusy(true)
    try { await fetchSkillRequest(r.id) } catch (err) { notifyError(err) }
    setBusy(false)
    onDone()
  }
  async function install() {
    const ok = await ask.confirm(
      `Install skill ${r.skill_name || r.source}? You have read SKILL.md (sha256 ${String(r.skill_sha256).slice(0, 12)}). `
      + 'It is installed exactly as read and granted to agents if its requirements are met.',
      { confirmLabel: 'Install & grant' })
    if (!ok) return
    setBusy(true)
    try {
      const done = await approveSkillRequest(r.id, r.skill_sha256)
      notify(done?.granted ? 'skill installed and granted' : `skill installed, not granted${done?.error ? `: ${done.error}` : ''}`)
      onDone()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }
  async function reject() {
    const reason = await ask.prompt(`Reject skill request ${r.source}? Reason (optional)`, '',
                                    { confirmLabel: 'Reject' })
    if (reason === null) return
    try { await rejectSkillRequest(r.id, reason); onDone() } catch (err) { notifyError(err) }
  }
  return (
    <div className="sbx-card bx-svc-req">
      <div className="bx-pkg-top">
        <Tag tone="pending">skill</Tag>
        <b className="mono">{r.skill_name || r.name || 'skill request'}</b>
        {r.project_slug && <Tag>{r.project_slug}</Tag>}
        <Tag>{r.status}</Tag>
        <span className="dim small">{ago(r.created_at)}</span>
      </div>
      <dl className="bx-kv small">
        <dt>source</dt><dd className="mono">{r.source}</dd>
        {r.reason && <><dt>why</dt><dd>{r.reason}</dd></>}
        {r.conversation_id && <><dt>conversation</dt><dd className="mono">{r.conversation_id}</dd></>}
        {st.status === 'pending' && <><dt>fetched</dt><dd>not yet: nothing has been downloaded</dd></>}
        {r.ref && <><dt>ref</dt><dd className="mono bx-hash">{r.ref}</dd></>}
        {r.description && <><dt>description</dt><dd>{r.description}</dd></>}
        {st.files.length > 0 && <><dt>files</dt><dd className="mono">{st.files.join('  ')}</dd></>}
        {(r.flags || []).length > 0 && <><dt>diff gates</dt>
          <dd>{r.flags.map((f, i) => (
            <div key={i} className="warn"><span className="mono">{f.file}</span>: {f.trigger}</div>))}</dd></>}
        {(r.requirements || []).length > 0 && <><dt>requires</dt>
          <dd>{r.requirements.map((q, i) => (
            <div key={i} className={q.met ? '' : 'warn'}>{q.met ? 'met' : 'NOT met'}: {q.reason}</div>))}</dd></>}
        {st.status === 'installed' && <><dt>installed</dt>
          <dd>{r.granted ? 'granted to agents' : 'installed, not granted'}
            {st.notGranted && r.error ? <span className="warn"> — {r.error}</span> : null}</dd></>}
      </dl>
      {r.error && st.status !== 'installed' && <div className="warn small">{r.error}</div>}
      {r.skill_md && st.status === 'fetched' && (
        <pre className="mono small bx-skill-md">{r.skill_md}</pre>)}
      {(st.canFetch || st.canRefetch || st.canReject) && (
        <div className="row">
          <span className="grow" />
          {st.canReject && <Button variant="ghost" danger onClick={reject}>Reject</Button>}
          {st.canRefetch && <Button variant="ghost" disabled={busy} onClick={fetchIt}>Fetch again</Button>}
          {st.canFetch && <Button disabled={busy} onClick={fetchIt}>Fetch &amp; read</Button>}
          {st.canInstall && <Button disabled={busy} onClick={install}>Install &amp; grant</Button>}
        </div>
      )}
    </div>
  )
}
