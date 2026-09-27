import { useEffect, useMemo, useState } from 'react'
import { Outlet } from 'react-router-dom'
import Page from '../components/Page.jsx'
import Tabs from '../components/Tabs.jsx'
import { Button, EmptyState, Input, Select, Tag } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { useAsk } from '../ask.jsx'
import { ago, ts } from '../format.js'
import {
  destroyBox, followBoxes, listBoxes, nukeShared, rebuildBase, startBox, stopBox, vmStatus,
} from '../boxes/api/vms.js'
import {
  buildVariant, createVariant, followBuilds, listImages, variantDockerfile,
} from '../boxes/api/images.js'
import { requestPackages } from '../boxes/api/packages.js'
import { listProfiles } from '../boxes/api/profiles.js'
import { deletePersist, getPersist, importPersist, listProjects } from '../boxes/api/persist.js'
import {
  applyBuildEvent, budgetSegments, buildNeedsReload, bytes, mb, mergeBuildRest, persistDaysLeft, sortBoxes,
  uptime, validPackage, validVersion, verLabel,
} from '../boxes/logic.js'
import {
  Confirm, LoadError, PlacementTag, ProjectList, RuntimeStatus, RuntimeTag, StateDot, Unavailable, useLoad,
} from '../boxes/ui.jsx'

// The VM manager: every box Jav3 runs (the shared turn box, project boxes,
// service boxes, image builders), the image variants they boot from, and the
// package catalogue those images are built from. It replaces the old VM chip
// in the bar, the shell sidebar's SandboxLine and the Work page's "Project
// VM" window (DESIGN-BOXES.md (e)).
//
// Every figure comes from the server (GET /api/vm/boxes, /api/vm/images);
// the host is a 4 GB Pi, so the RAM budget bar is the first thing on the page.


export default function Vms() {
  return (
    <Page variant="fill" title="VMs" className="review-shell"
          actions={(
            <Tabs label="VM sections" items={[
              { to: '/vms', end: true, label: 'Boxes' },
              { to: '/vms/images', label: 'Images' },
              { to: '/vms/catalogue', label: 'Catalogue' },
            ]} />
          )}>
      <div className="review-body"><Outlet /></div>
    </Page>
  )
}

// ---- Boxes -------------------------------------------------------------------

const KIND_TEXT = {
  shared: 'shared turn box',
  project: 'project box',
  service: 'service box',
  builder: 'image builder',
}

export function Boxes() {
  const { data, error, reload } = useLoad(listBoxes, { every: 8000 })
  const [dlg, setDlg] = useState(null)       // {verb, box}

  const boxes = useMemo(() => sortBoxes(data?.boxes), [data])
  // box_up / box_down on the shared stream: refetch (the poll is the fallback)
  useEffect(() => followBoxes(() => reload()), [reload])

  async function act(verb, box, flag) {
    try {
      if (verb === 'start') await startBox(box.id)
      else if (verb === 'stop') await stopBox(box.id)
      else if (verb === 'destroy') await destroyBox(box.id, flag)
      else if (verb === 'nuke') {
        if (box.inflight > 0) {
          notify(`${box.inflight} turn(s) in flight — wait for them to finish before nuking.`)
          return
        }
        await nukeShared()
      }
      setDlg(null)
      reload()
    } catch (e) {
      notifyError(e)
      if (e.status === 409) setDlg(null)
    }
  }

  return (
    <div className="bx-page">
      <LoadError error={error} />
      {data?.legacy && (
        <div className="bx-unavail dim small">This server has no box manager yet: only the
          shared guest is shown, from <code>/api/vm/status</code>.</div>
      )}
      {data && !data.legacy && !data.enabled && (
        <div className="bx-unavail dim small">Boxes are switched off on the server
          (<code>vm_boxes_enabled</code>): every project runs in the one shared box.</div>
      )}

      <Budget data={data} />
      {data && !data.legacy && <RuntimeStatus runtimes={data.runtimes} />}

      <section className="sbx-sec">
        <div className="sbx-sec-head"><h3>Boxes</h3>
          <span className="sec-count">{boxes.length}</span></div>
        {!data && !error && <div className="dim">…</div>}
        {data && boxes.length === 0 && <EmptyState>no boxes</EmptyState>}
        <div className="bx-table" role="table" aria-label="boxes">
          {boxes.length > 0 && (
            <div className="bx-tr bx-th" role="row">
              <span role="columnheader">Box</span>
              <span role="columnheader">Runtime</span>
              <span role="columnheader">Image</span>
              <span role="columnheader">RAM</span>
              <span role="columnheader">CPU</span>
              <span role="columnheader">Disk</span>
              <span role="columnheader">Up</span>
              <span role="columnheader" />
            </div>
          )}
          {boxes.map((b) => (
            <div key={b.id} className={`bx-tr${b.state === 'running' ? '' : ' off'}`} role="row">
              <span className="bx-box" role="cell">
                <StateDot state={b.state} inflight={b.inflight} />
                <span className="bx-box-main">
                  <span className="mono">{b.id}</span>
                  <span className="dim small">
                    {KIND_TEXT[b.kind] || b.kind}
                    {b.project ? ` · ${b.project}` : ''}
                    {b.state === 'running' && b.inflight > 0 ? ` · ${b.inflight} turn(s) now` : ''}
                    {b.state !== 'running' ? ` · ${b.state}` : ''}
                  </span>
                  {b.kind === 'service' && <PlacementTag placement={b.placement} />}
                </span>
              </span>
              <span role="cell"><RuntimeTag runtime={b.runtime} /></span>
              <span role="cell" className="mono small">
                {b.image?.variant || 'main'}{b.image?.version ? ` ${b.image.version}` : ' (active)'}</span>
              <span role="cell" className="small">
                {mb(b.mem_mb)}
                {b.rss_bytes != null && <span className="dim"> · {bytes(b.rss_bytes)} used</span>}</span>
              <span role="cell" className="small">{b.cpu_pct != null ? `${b.cpu_pct}%` : '–'}</span>
              <span role="cell" className="small" title="overlay · /srv data">
                {bytes(b.disk?.overlay_bytes)}
                {b.disk?.data_bytes != null && <span className="dim"> · {bytes(b.disk.data_bytes)} data</span>}</span>
              <span role="cell" className="small">{b.state === 'running' ? uptime(b.uptime_s) : '–'}</span>
              <span role="cell" className="bx-actions">
                {b.state === 'running'
                  ? <Button variant="ghost" disabled={data?.legacy}
                            onClick={() => setDlg({ verb: 'stop', box: b })}>Stop</Button>
                  : <Button variant="ghost" disabled={data?.legacy}
                            onClick={() => setDlg({ verb: 'start', box: b })}>Start</Button>}
                {b.kind === 'shared'
                  ? <Button variant="ghost" danger disabled={b.state !== 'running'}
                            onClick={() => setDlg({ verb: 'nuke', box: b })}>Nuke</Button>
                  : <Button variant="ghost" danger
                            onClick={() => setDlg({ verb: 'destroy', box: b })}>Destroy</Button>}
              </span>
            </div>
          ))}
        </div>
      </section>

      {data && !data.legacy && data.enabled && <WarmUp boxes={boxes} onDone={reload} />}

      <PersistRetirement />

      <BoxDialog dlg={dlg} budget={data?.budget} runtimes={data?.runtimes}
                 onClose={() => setDlg(null)} onAct={act} />
    </div>
  )
}

function Budget({ data }) {
  const b = data?.budget
  if (!data) return null
  const cap = b?.ram_mb_cap || 0
  const host = b?.host_ram_mb || 0
  const { segs, used, over } = budgetSegments(data.boxes, cap || host || 4096)
  const scale = cap || host || 4096
  return (
    <section className="sbx-card bx-budget">
      <div className="bx-budget-head">
        <b>RAM</b>
        <span className={over ? 'error small' : 'small'}>
          {mb(b?.ram_mb_used ?? used)} reserved of {cap ? `${mb(cap)} guest budget` : 'the host'}
        </span>
        {host > 0 && (
          <span className="dim small">host {mb(host)} · Jav3 and the OS keep the rest</span>
        )}
        <span className="grow" />
        {b && (
          <span className="dim small">
            boxes {b.boxes}/{b.boxes_cap} · project boxes {b.project_boxes}/{b.project_boxes_cap}
          </span>
        )}
      </div>
      <div className="bx-bar" role="img"
           aria-label={`${used} of ${scale} MB reserved`}>
        {segs.map((s) => (
          <span key={s.id} className={`bx-seg k-${s.kind}${s.running ? ' on' : ''}`}
                style={{ width: `${Math.min(100, (s.mb / scale) * 100)}%` }}
                title={`${s.id}: ${s.mb} MB${s.running ? '' : ' (reserved, stopped)'}`} />
        ))}
      </div>
      <div className="bx-legend dim small">
        <span><i className="k-shared" /> shared</span>
        <span><i className="k-project" /> project</span>
        <span><i className="k-service" /> service</span>
        <span><i className="k-builder" /> builder</span>
        <span>faded = reserved but stopped (every reservation counts)</span>
      </div>
    </section>
  )
}

// Exactly what each verb does, in the words the operator decides on.
function BoxDialog({ dlg, budget, runtimes, onClose, onAct }) {
  if (!dlg) return null
  const { verb, box: b } = dlg
  const who = `${b.id}${b.project ? ` (${b.project})` : ''}`
  if (verb === 'start') {
    return (
      <Confirm open title={`Start ${who}?`} confirmLabel="Start" onClose={onClose}
               onConfirm={() => onAct('start', b)}>
        <p>Boots the {KIND_TEXT[b.kind] || b.kind} from <code>{b.image?.variant || 'main'}</code>{' '}
          with {mb(b.mem_mb)} of RAM{b.runtime === 'docker' ? ' as a Docker container (shares the host kernel)' : ''}.</p>
        <p>Its reservation already counts against the guest budget
          {budget ? ` (${mb(budget.ram_mb_used)} of ${mb(budget.ram_mb_cap)} reserved)` : ''}. If a cap
          is reached the server refuses; nothing is queued.</p>
        {b.kind === 'service' && <p>Every approved service placed in it starts with it.</p>}
        {b.runtime === 'docker' && runtimes && !runtimes.docker.available && (
          <p className="error small">Docker is unavailable on this host
            {runtimes.docker.reason ? `: ${runtimes.docker.reason}` : ''}. The start will fail.</p>)}
        {b.runtime === 'docker' && runtimes?.docker.weak && (
          <p className="warn">Weak isolation: this container would run without a user namespace.
            {runtimes.docker.warnings.length ? ` ${runtimes.docker.warnings.join(' ')}` : ''}</p>)}
      </Confirm>
    )
  }
  if (verb === 'stop') {
    return (
      <Confirm open title={`Stop ${who}?`} confirmLabel="Stop" danger onClose={onClose}
               onConfirm={() => onAct('stop', b)}>
        <p>Kills the guest. Its slot and {mb(b.mem_mb)} reservation stay, so it can be started again.</p>
        {b.kind === 'service'
          ? <p>Every service in it stops until the box is started again, whatever its restart
              policy. Its <code>/srv</code> data disk is kept.</p>
          : b.kind === 'builder'
            ? <p>The image build running in it is abandoned; no new version is recorded.</p>
            : <p>Anything installed or written inside the VM since it booted is gone at the
                next boot. Project files live on the host and are not touched.
                {b.inflight > 0 ? ` ${b.inflight} turn(s) running in it now will fail.` : ''}</p>}
      </Confirm>
    )
  }
  if (verb === 'nuke') {
    return (
      <Confirm open title="Nuke the shared box?" confirmLabel="Nuke it" danger onClose={onClose}
               onConfirm={() => onAct('nuke', b)}>
        <p>Its overlay disk is discarded and it reboots fresh from the golden image. Anything the
          agent installed in it since boot is lost. Refused while a turn is in flight
          {b.inflight > 0 ? ` (${b.inflight} now)` : ''}.</p>
      </Confirm>
    )
  }
  return (
    <Confirm open title={`Destroy ${who}?`} confirmLabel="Destroy" danger onClose={onClose}
             onConfirm={(del) => onAct('destroy', b, del)}
             check={b.kind === 'service'
               ? `Also delete its /srv data disk${b.disk?.data_bytes != null ? ` (${bytes(b.disk.data_bytes)})` : ''} — this cannot be undone`
               : null}>
      <p>Stops it, releases its slot and its {mb(b.mem_mb)} reservation, and deletes its
        directory on the host (overlay disk, console log, EFI vars).</p>
      {b.kind === 'project' && <p>The next turn in <b>{b.project}</b> allocates a fresh box, if its
        profile still asks for one.</p>}
      {b.kind === 'service' && <p>The approved service definitions are kept on the host; the box is
        rebuilt from them the next time a service in it starts. Unticked, the data disk is kept.</p>}
    </Confirm>
  )
}

// Warm a project box before its first turn (contract: p-<slug> may be
// started before it is allocated). Only projects whose profile asks for a
// separate box are offered.
function WarmUp({ boxes, onDone }) {
  const [profiles, setProfiles] = useState(null)
  const [pick, setPick] = useState('')
  useEffect(() => { listProfiles().then(setProfiles).catch(() => setProfiles([])) }, [])
  const have = new Set(boxes.map((b) => b.id))
  const slugs = (profiles || []).filter((p) => p.separate_box)
    .flatMap((p) => p.projects || []).filter((s) => !have.has(`p-${s}`))
  if (!slugs.length) return null
  async function go() {
    try { await startBox(`p-${pick}`); setPick(''); onDone() } catch (e) { notifyError(e) }
  }
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Warm up a project box</h3></div>
      <div className="row">
        <Select aria-label="project" value={pick} placeholder="project…"
                onChange={(e) => setPick(e.target.value)} options={slugs} />
        <Button variant="ghost" disabled={!pick} onClick={go}>Start its box</Button>
        <span className="dim small">boots it now instead of on the project's next turn</span>
      </div>
    </section>
  )
}

// ---- /persist retirement (decision 0.3) --------------------------------------
// Where the old persist controls were (the Work page's Project VM window) is
// gone with that window; the disks that still exist are listed here with the
// one action left: import into the project's service box /srv.
function PersistRetirement() {
  const [rows, setRows] = useState(null)
  const [dlg, setDlg] = useState(null)
  const load = () => listProjects().then((ps) => Promise.all(ps.map((p) =>
    getPersist(p.slug).then((v) => ({ ...v, slug: p.slug, name: p.name })).catch(() => null))))
    .then((vs) => setRows(vs.filter((v) => v && (v.disk?.exists || v.approved || v.import))))
    .catch(() => setRows([]))
  useEffect(() => { load() }, [])
  if (!rows || !rows.length) return null
  async function run(verb, slug) {
    try {
      if (verb === 'import') await importPersist(slug)
      else await deletePersist(slug)
      setDlg(null); load()
    } catch (e) { notifyError(e) }
  }
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Old /persist disks</h3>
        <span className="sec-count">{rows.length}</span></div>
      <p className="dim small bx-note">/persist is retired: no new approvals. Import a project's
        disk into its service box's <code>/srv</code>; the old disk is deleted 30 days after the
        import (or now, if you delete it).</p>
      <ul className="staged-list rev-list">
        {rows.map((r) => {
          const days = persistDaysLeft(r)
          const imported = r.imported_at
          const st = r.import?.state || null      // pending | done | failed
          return (
            <li key={r.slug}>
              <span className="grow">
                <b>{r.name || r.slug}</b>{' '}
                <span className="dim small">
                  {r.disk?.exists ? `${bytes(r.disk.bytes_used)} on /persist` : 'no disk'}
                  {imported ? ` · imported ${ago(imported)}` : ''}
                </span>
                {r.import?.error && <span className="error small bx-row-err">import: {r.import.error}</span>}
              </span>
              {st === 'pending' && <Tag tone="running" title="copying into the service box's /srv">importing…</Tag>}
              {st === 'failed' && <Tag tone="error">import failed</Tag>}
              {days != null && (
                <Tag tone="pending" title={`deleted after ${ts(r.delete_after)}`}>
                  deleted in {days} day{days === 1 ? '' : 's'}</Tag>)}
              {st !== 'pending' && st !== 'done' && days == null && (
                <Button variant="ghost" disabled={!r.disk?.exists}
                        onClick={() => setDlg({ verb: 'import', r })}>
                  {st === 'failed' ? 'Retry import…' : 'Import into service box…'}</Button>)}
              {r.disk?.exists && (
                <Button variant="ghost" danger onClick={() => setDlg({ verb: 'delete', r })}>Delete…</Button>)}
            </li>
          )
        })}
      </ul>
      <Confirm open={dlg?.verb === 'import'} title={`Import ${dlg?.r.slug}'s /persist?`}
               confirmLabel="Import" onClose={() => setDlg(null)}
               onConfirm={() => run('import', dlg.r.slug)}>
        <p>Copies everything on the project's <code>/persist</code> disk into its service box's
          <code> /srv</code> (the box is created if it does not exist). Nothing in it runs until a
          service you approve uses it.</p>
        <p>The old disk is kept, read-only, for 30 days, then deleted.</p>
      </Confirm>
      <Confirm open={dlg?.verb === 'delete'} title={`Delete ${dlg?.r.slug}'s /persist disk?`}
               confirmLabel="Delete" danger onClose={() => setDlg(null)}
               onConfirm={() => run('delete', dlg.r.slug)}>
        <p>Deletes the disk and everything on it now{dlg?.r.disk?.bytes_used != null
          ? ` (${bytes(dlg.r.disk.bytes_used)})` : ''}. An import already made is not affected.
          This cannot be undone.</p>
      </Confirm>
    </section>
  )
}

// ---- Images ------------------------------------------------------------------

export function Images() {
  const { data, error, unavailable, reload } = useLoad(listImages, { every: 0 })
  const [bs, setBs] = useState(null)       // the build panel: logic.buildState
  const [dlg, setDlg] = useState(null)

  // seed from the REST read; the `vm-images` topic on the shared stream
  // carries the rest (start, boot, log lines, done, resolved)
  useEffect(() => { if (data) setBs((cur) => mergeBuildRest(cur, data.build)) }, [data])
  useEffect(() => followBuilds((ev) => {
    setBs((cur) => applyBuildEvent(cur, ev))
    if (buildNeedsReload(ev)) reload()
  }), [reload])
  const building = !!bs?.running
  // poll while a build runs, in case the stream is down
  useEffect(() => {
    if (!building) return undefined
    const t = setInterval(reload, 5000)
    return () => clearInterval(t)
  }, [building, reload])

  async function build(v) {
    try { await buildVariant(v); setDlg(null); reload() } catch (e) { notifyError(e) }
  }

  return (
    <div className="bx-page">
      <BaseImage />
      {unavailable && <Unavailable what="The image manager" />}
      <LoadError error={error} />
      {bs && (bs.running || bs.last || bs.log.length > 0) && <BuildPanel bs={bs} />}
      <section className="sbx-sec">
        <div className="sbx-sec-head"><h3>Variants</h3>
          <span className="sec-count">{data?.variants.length ?? '…'}</span></div>
        {(data?.variants || []).map((v) => (
          <Variant key={v.name} v={v} building={building}
                   onBuild={() => setDlg(v)} />
        ))}
      </section>
      {data && <AddPackages variants={data.variants} onDone={reload} />}
      <Confirm open={!!dlg} title={`Build a new version of ${dlg?.name}?`} confirmLabel="Build"
               onClose={() => setDlg(null)} onConfirm={() => build(dlg.name)}>
        <p>A builder box (about 1 GB of RAM, counted against the budget) boots
          from <code>{dlg?.from}</code>, runs the recipe through the monitored proxy, records the
          process baseline, and freezes the result as a new version of <code>{dlg?.name}</code>.</p>
        {(dlg?.layer_packages || []).length > 0 && (
          <p>This layer installs: <span className="mono small">{dlg.layer_packages.join('  ')}</span></p>)}
        <p>Boxes already running keep the version they booted; each picks up the new one at its
          next boot. Used by: <ProjectList slugs={dlg?.used_by} empty="no project yet" />.</p>
      </Confirm>
    </div>
  )
}

// The running (or last) build: phase, box, and the builder's log lines. The
// lines come from the builder guest: one text node.
function BuildPanel({ bs }) {
  return (
    <section className="sbx-card bx-building-card">
      <div className="bx-building">
        {bs.running && <span className="run-dot running" aria-hidden="true" />}
        {bs.running
          ? <>Building <b className="mono">{bs.variant}</b>
              <span className="dim"> — {bs.phase || 'working'}{bs.box ? ` in ${bs.box}` : ''}
                {bs.mode ? ` · ${bs.mode}` : ''}</span></>
          : bs.last
            ? <>Last build of <b className="mono">{bs.last.variant}</b>{' '}
                {bs.last.ok
                  ? <Tag tone="done">built{bs.last.version != null ? ` ${verLabel(bs.last.version)}` : ''}</Tag>
                  : <Tag tone="error">failed</Tag>}
                {bs.last.error && <span className="error small"> {bs.last.error}</span>}</>
            : <span className="dim">build log</span>}
      </div>
      {bs.resolved && (
        <div className="dim small">package dry-run: {bs.resolved.count} resolved
          {bs.resolved.error ? ` — ${bs.resolved.error}` : ''}</div>)}
      {bs.log.length > 0 && (
        <details open={bs.running}>
          <summary className="small">log ({bs.log.length} line{bs.log.length === 1 ? '' : 's'}) —
            written by the builder guest</summary>
          <pre className="mono small bx-log-tail">{bs.log.join('\n')}</pre>
        </details>
      )}
    </section>
  )
}

function Variant({ v, building, onBuild }) {
  const versions = v.versions || []
  const [docker, setDocker] = useState(null)
  const loadDocker = (e) => {
    if (!e.currentTarget.open || docker) return
    variantDockerfile(v.name).then((r) => setDocker(r)).catch((err) => setDocker({ error: err }))
  }
  return (
    <div className="sbx-card bx-variant">
      <div className="bx-variant-head">
        <b className="mono">{v.name}</b>
        {v.builtin && <Tag>built-in</Tag>}
        {v.needs_build && <Tag tone="pending" title="its recipe changed since the active version was built">
          needs a build</Tag>}
        <span className="dim small">from {v.from}</span>
        {v.min_mem_mb ? <span className="dim small">· needs ≥ {mb(v.min_mem_mb)}</span> : null}
        <span className="grow" />
        <Button variant="ghost" disabled={building} onClick={onBuild}>Build new version</Button>
      </div>
      <div className="small bx-used">used by: <ProjectList slugs={v.used_by} empty="no project" /></div>
      {(v.layer_packages || []).length > 0 && (
        <div className="small">this layer: <span className="mono">{v.layer_packages.join('  ')}</span></div>)}
      <details className="bx-recipe">
        <summary className="small">recipe
          {v.recipe_sha256 && <span className="dim mono"> · {String(v.recipe_sha256).slice(0, 12)}</span>}</summary>
        <pre className="mono small">{v.recipe || '(empty)'}</pre>
      </details>
      <details className="bx-recipe" onToggle={loadDocker}>
        <summary className="small">Dockerfile <span className="dim">(the docker runtime's build)</span></summary>
        {!docker && <div className="dim small">…</div>}
        {docker?.error && <div className="error small">{docker.error.detail || String(docker.error)}</div>}
        {docker?.dockerfile != null && <pre className="mono small">{docker.dockerfile}</pre>}
      </details>
      {versions.length === 0
        ? <div className="dim small">never built</div>
        : (
          <ul className="staged-list rev-list bx-versions">
            {versions.map((x) => (
              <li key={x.version}>
                <span className="mono">{verLabel(x.version)}</span>
                {x.active && <Tag tone="done">active</Tag>}
                <Tag tone={x.status === 'failed' ? 'error' : x.status === 'built' || x.status === 'ready' ? undefined : 'running'}>
                  {x.status}</Tag>
                <span className="dim small">on {x.base_version}</span>
                <span className="dim small">{bytes(x.size_bytes)}</span>
                <span className="dim small">{ts(x.built_at)}</span>
                <span className="grow" />
                {(x.in_use_by || []).length > 0 && (
                  <span className="small">in use by boxes <ProjectList slugs={x.in_use_by} /></span>)}
              </li>
            ))}
          </ul>
        )}
    </div>
  )
}

// The shared guest's golden image and its rebuild, kept from the old VM chip.
function BaseImage() {
  const [s, setS] = useState(null)
  const [busy, setBusy] = useState(false)
  const ask = useAsk()
  useEffect(() => { vmStatus().then(setS).catch(() => setS(null)) }, [])
  if (!s) return null
  async function rebuild() {
    if (!await ask.confirm('Rebuild the base image from scratch?', {
      body: 'Builds a new base-vN from the Debian cloud image (about 20 minutes on the Pi). '
        + 'Every variant is replayed on the new base afterwards. Running boxes keep their '
        + 'current image until they restart.',
      confirmLabel: 'Rebuild', danger: true })) return
    setBusy(true)
    try { await rebuildBase(); notify('base image rebuild started') } catch (e) { notifyError(e) }
    setBusy(false)
  }
  return (
    <section className="sbx-card bx-base">
      <b>Base image</b>
      <span className={s.image_stale ? 'warn' : 'mono small'}>{s.image_version || '?'}</span>
      {s.image_built_at && <span className="dim small">built {String(s.image_built_at).slice(0, 10)}</span>}
      {s.image_stale && <Tag tone="pending">stale — rebuild suggested</Tag>}
      <span className="grow" />
      <Button variant="ghost" disabled={busy} onClick={rebuild}>
        {busy ? 'Rebuilding…' : 'Rebuild base…'}</Button>
    </section>
  )
}

// The package-manager form: operator requests land in the catalogue (source
// operator), pending like any other, and the build is a separate click.
// "New variant" creates one from its packages instead (POST /api/vm/images).
function AddPackages({ variants, onDone }) {
  const blank = { manager: 'apt', package: '', version: '' }
  const [target, setTarget] = useState(variants[0]?.name || 'main')
  const [newName, setNewName] = useState('')
  const [from, setFrom] = useState('main')
  const [rows, setRows] = useState([{ ...blank }])
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const isNew = target === '__new__'
  const users = variants.find((v) => v.name === target)?.used_by || []

  const bad = rows.filter((r) => r.package).some((r) =>
    !validPackage(r.manager, r.package) || !validVersion(r.version))
  const filled = rows.filter((r) => r.package.trim())
  const ok = filled.length > 0 && !bad && (!isNew || /^[a-z][a-z0-9-]{1,30}$/.test(newName))
    && (isNew || reason.trim())

  const set = (i, k, v) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, [k]: v } : r)))

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    try {
      const pkgs = filled.map((r) => ({
        manager: r.manager, package: r.package.trim(), version: r.version.trim() || null }))
      if (isNew) {
        await createVariant({ name: newName, from, packages: pkgs })
        notify(`variant ${newName} created — build it to use it`)
      } else {
        const r = await requestPackages({ packages: pkgs, reason: reason.trim(), target_variant: target })
        const skipped = r.skipped.map((x) => `${x.package}: ${x.error}`)
        notify(`${r.packages.length} package request(s) filed into ${r.target_variant || target}`
          + ' — approve them in the Catalogue'
          + (skipped.length ? `. Skipped: ${skipped.join('; ')}` : ''))
      }
      setRows([{ ...blank }]); setReason(''); setNewName('')
      onDone()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Add packages</h3></div>
      <form className="sbx-card bx-form" onSubmit={submit}>
        <div className="row">
          <Select label="Into" value={target} onChange={(e) => setTarget(e.target.value)}
                  options={[...variants.map((v) => ({ value: v.name, label: v.name })),
                    { value: '__new__', label: 'a new variant…' }]} />
          {isNew && <>
            <Input label="Name" value={newName} placeholder="e.g. data"
                   onChange={(e) => setNewName(e.target.value.toLowerCase())} />
            <Select label="From" value={from} onChange={(e) => setFrom(e.target.value)}
                    options={['base', ...variants.map((v) => v.name)]} />
          </>}
        </div>
        {!isNew && (
          <div className="small bx-used">installs into <code>{target}</code> — used by:{' '}
            <ProjectList slugs={users} empty="no project yet" /></div>
        )}
        {rows.map((r, i) => {
          const badName = r.package && !validPackage(r.manager, r.package)
          return (
            <div className="row bx-pkg-row" key={i}>
              <Select aria-label="manager" value={r.manager}
                      onChange={(e) => set(i, 'manager', e.target.value)} options={['apt', 'pip', 'npm']} />
              <Input aria-label="package" placeholder="package" value={r.package} spellCheck={false}
                     aria-invalid={badName || undefined}
                     onChange={(e) => set(i, 'package', e.target.value)} />
              <Input aria-label="version" placeholder="version (optional)" value={r.version}
                     spellCheck={false} onChange={(e) => set(i, 'version', e.target.value)} />
              {rows.length > 1 && (
                <Button variant="icon" aria-label="remove row"
                        onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>×</Button>)}
              {badName && <span className="error small">a bare package name — no URLs, paths or flags</span>}
            </div>
          )
        })}
        <div className="row">
          <Button variant="ghost" onClick={() => setRows((rs) => [...rs, { ...blank }])}>+ package</Button>
        </div>
        {!isNew && (
          <Input label="Reason" value={reason} placeholder="why this image needs it"
                 onChange={(e) => setReason(e.target.value)} />
        )}
        <div className="row">
          <span className="dim small grow">The host builds the install command itself; it never
            runs a typed string. Nothing changes in an image until a new version is built.</span>
          <Button type="submit" disabled={!ok || busy}>
            {isNew ? 'Create variant' : 'File requests'}</Button>
        </div>
      </form>
    </section>
  )
}

