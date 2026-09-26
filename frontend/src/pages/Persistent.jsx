import { useEffect, useMemo, useState } from 'react'
import { Button, EmptyState, Tag, Toggle } from '../components/index.js'
import { notifyError } from '../notify.js'
import { ago } from '../format.js'
import { followProcs, listProcesses } from '../boxes/api/procs.js'
import { listServices, revokeService, startService, stopService } from '../boxes/api/services.js'
import {
  bytes, connCheck, flattenTree, mergeProcs, normBoxProcs, sortBoxes, treeTotals,
} from '../boxes/logic.js'
import { Confirm, LoadError, PlacementTag, Unavailable, useLoad } from '../boxes/ui.jsx'

// Security > Persistent: what is running in every box that is not the OS —
// approved services, what run_code left behind, and anything unexpected —
// with each process's connections, guest-reported bytes beside the bytes the
// host itself metered (the proxy and the port relay). A guest can lie about
// /proc; it cannot hide from the proxy, so a disagreement is the signal.
//
// Live: topic `procs` on the ONE shared stream (events.js); the REST read
// seeds it. Every string in a process row is guest-reported: text nodes only.

const TAG_TONE = { service: 'done', unexpected: 'error', run_code: 'running' }

export default function Persistent() {
  const { data, error, unavailable, setData } = useLoad(
    () => listProcesses().then((bs) => sortBoxes(bs.map(normBoxProcs))))
  const svc = useLoad(() => listServices(), { every: 20000 })
  const [onlyOdd, setOnlyOdd] = useState(false)
  const [dlg, setDlg] = useState(null)

  useEffect(() => followProcs((ev) => setData((prev) => mergeProcs(prev || [], ev))), [setData])

  const services = useMemo(() => Object.fromEntries(
    (svc.data?.services || []).map((s) => [s.id, s])), [svc.data])

  async function stop(s) {
    try { await stopService(s.id); svc.reload() } catch (e) { notifyError(e) }
  }
  async function start(s) {
    try { await startService(s.id); svc.reload() } catch (e) { notifyError(e) }
  }
  async function revoke(s, del) {
    try { await revokeService(s.id, del); setDlg(null); svc.reload() } catch (e) { notifyError(e) }
  }

  if (unavailable) return <div className="bx-page"><Unavailable what="The process view" /></div>
  const boxes = data || []
  const all = boxes.reduce((t, b) => {
    const x = treeTotals(b.tree)
    return { procs: t.procs + x.procs, unexpected: t.unexpected + x.unexpected, mism: t.mism + x.mismatches }
  }, { procs: 0, unexpected: 0, mism: 0 })

  return (
    <div className="bx-page">
      <LoadError error={error} />
      <div className="net-top">
        <div className="net-counts">
          <span><b>{boxes.length}</b> boxes</span>
          <span><b>{all.procs}</b> processes</span>
          <span className={all.unexpected ? 'bx-red' : ''}><b>{all.unexpected}</b> unexpected</span>
          <span className={all.mism ? 'bx-red' : ''}><b>{all.mism}</b> byte mismatches</span>
        </div>
        <span className="grow" />
        <Toggle checked={onlyOdd} onChange={setOnlyOdd} label="only boxes with something unexpected"
                onText="only unexpected" offText="every box" />
      </div>
      <ServiceList services={svc.data?.services} onStop={stop} onStart={start}
                   onRevoke={(x) => setDlg(x)} />
      {!data && !error && <div className="dim">…</div>}
      {data && boxes.length === 0 && <EmptyState pad>no box is reporting</EmptyState>}
      {boxes.filter((b) => !onlyOdd || treeTotals(b.tree).unexpected || treeTotals(b.tree).mismatches)
        .map((b) => (
          <BoxTree key={b.box_id} b={b} services={services}
                   onStop={stop} onRevoke={(s) => setDlg(s)} />
        ))}
      <Confirm open={!!dlg} title={`Revoke service ${dlg?.name}?`} confirmLabel="Revoke" danger
               onClose={() => setDlg(null)} onConfirm={(del) => revoke(dlg, del)}
               check="Also delete its /srv data — this cannot be undone">
        <p>Deletes the approved definition and destroys the box it runs in
          {dlg?.box_id ? <> (<code>{dlg.box_id}</code>)</> : null}: killing the VM is the
          authoritative stop. Other services placed in the same box restart in a fresh one.</p>
        <p>To run it again the agent has to file a new request.</p>
      </Confirm>
    </div>
  )
}

// Every approved service, running or not, with the placement it was
// approved with (decision 0.1: shown wherever a service is listed).
function ServiceList({ services, onStop, onStart, onRevoke }) {
  const approved = (services || []).filter((s) => s.status === 'approved')
  if (!approved.length) return null
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Services</h3>
        <span className="sec-count">{approved.length}</span></div>
      <ul className="staged-list rev-list">
        {approved.map((s) => (
          <li key={s.id}>
            <span className={`run-dot ${s.state === 'running' ? 'done' : s.state === 'failed' ? 'error' : ''}`}
                  aria-hidden="true" />
            <b className="mono">{s.name}</b>
            <span className="dim small">#{s.id} · {s.project_slug}{s.box_id ? ` · ${s.box_id}` : ''}</span>
            <PlacementTag placement={s.placement} />
            <Tag tone={s.state === 'running' ? 'done' : s.state === 'failed' ? 'error'
              : s.state === 'unreported' ? 'pending' : undefined}>{s.state}</Tag>
            {(s.expose_ports || []).map((p) => (
              <Tag key={p.port} title="exposed port">{p.port} → {p.bind === 'lan' ? 'LAN' : 'host'}</Tag>))}
            <span className="grow" />
            {s.state === 'running'
              ? <Button variant="ghost" onClick={() => onStop(s)}>Stop</Button>
              : <Button variant="ghost" onClick={() => onStart(s)}>Start</Button>}
            <Button variant="ghost" danger onClick={() => onRevoke(s)}>Revoke</Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function BoxTree({ b, services, onStop, onRevoke }) {
  const [collapsed, setCollapsed] = useState(() => new Set())
  const t = useMemo(() => treeTotals(b.tree), [b.tree])
  const rows = useMemo(() => flattenTree(b.tree, collapsed), [b.tree, collapsed])
  const flip = (key) => setCollapsed((c) => {
    const n = new Set(c)
    if (n.has(key)) n.delete(key); else n.add(key)
    return n
  })
  return (
    <details className={`sbx-card bx-tree${t.unexpected ? ' odd' : ''}`} open>
      <summary className="bx-tree-head">
        <b className="mono">{b.box_id}</b>
        <span className="dim small">{b.kind}{b.project ? ` · ${b.project}` : ''}</span>
        {b.stale && <Tag tone="pending" title="the box has not reported recently">stale</Tag>}
        <span className="dim small">reported {ago(b.reported_at) || 'never'}</span>
        <span className="grow" />
        <span className="small bx-totals">
          {t.procs} proc{t.procs === 1 ? '' : 's'}
          {t.service ? ` · ${t.service} service` : ''}
          {t.run_code ? ` · ${t.run_code} run_code` : ''}
          {t.unexpected ? <b className="bx-red"> · {t.unexpected} unexpected</b> : null}
          {' · '}{t.out} out / {t.in} in
          {' · '}guest {bytes(t.guest_out)}↑ {bytes(t.guest_in)}↓
          {' · '}host {bytes(t.host_out)}↑ {bytes(t.host_in)}↓
          {t.mismatches ? <b className="bx-red"> · {t.mismatches} mismatch</b> : null}
        </span>
      </summary>
      {rows.length === 0 && <div className="dim small bx-none">nothing beyond the OS baseline</div>}
      <div className="bx-procs">
        {rows.map((r) => (
          <ProcRow key={r.key} r={r} svc={services[r.node.service_id]}
                   onFlip={() => flip(r.key)} onStop={onStop} onRevoke={onRevoke} />
        ))}
      </div>
    </details>
  )
}

function ProcRow({ r, svc, onFlip, onStop, onRevoke }) {
  const n = r.node
  const pad = { paddingLeft: `${r.depth * 18 + 4}px` }
  return (
    <>
      <div className={`bx-proc${n.tag === 'unexpected' ? ' odd' : ''}`}>
        <span className="bx-proc-main" style={pad}>
          {r.hasChildren
            ? <button type="button" className="bx-twist" aria-expanded={r.open}
                      aria-label={r.open ? 'collapse' : 'expand'} onClick={onFlip}>
                {r.open ? '▾' : '▸'}</button>
            : <span className="bx-twist" />}
          <span className="mono small dim">{n.pid}</span>
          <Tag tone={TAG_TONE[n.tag]} className={n.tag === 'unexpected' ? 'bx-odd-tag' : ''}>
            {n.tag === 'service' && n.service_id ? `service #${n.service_id}` : (n.tag || 'process')}</Tag>
          <span className="mono small bx-cmd" title={n.cmd || n.exe}>{n.cmd || n.exe}</span>
        </span>
        <span className="dim small">{n.user}</span>
        <span className="small">{bytes(n.rss)}</span>
        <span className="small">{n.cpu_pct != null ? `${n.cpu_pct}%` : '–'}</span>
        <span className="bx-actions">
          {svc && <>
            <PlacementTag placement={svc.placement} />
            <Button variant="ghost" disabled={svc.state !== 'running'} onClick={() => onStop(svc)}>Stop</Button>
            <Button variant="ghost" danger onClick={() => onRevoke(svc)}>Revoke</Button>
          </>}
        </span>
      </div>
      {(n.conns || []).map((c, i) => <ConnRow key={i} c={c} depth={r.depth} />)}
    </>
  )
}

function ConnRow({ c, depth }) {
  const check = connCheck(c)
  const peer = c.host || (c.raddr ? `${c.raddr}:${c.rport}` : '—')
  return (
    <div className={`bx-conn${check === 'mismatch' ? ' mismatch' : ''}`}
         style={{ paddingLeft: `${depth * 18 + 44}px` }}>
      <span className="mono small">{c.dir === 'in' ? '⇠ in ' : '⇢ out'} {c.proto}</span>
      <span className="mono small bx-cmd" title={`${c.laddr}:${c.lport} ${c.dir === 'in' ? '←' : '→'} ${peer}`}>
        :{c.lport} {c.dir === 'in' ? '←' : '→'} {peer}</span>
      <span className="dim small">{c.state}</span>
      <span className="small" title="guest-reported: sent / received">
        guest {bytes(c.guest_bytes_out)}↑ {bytes(c.guest_bytes_in)}↓</span>
      <span className="small" title="host-metered (proxy / relay): sent / received">
        host {c.host_bytes_out != null ? `${bytes(c.host_bytes_out)}↑ ${bytes(c.host_bytes_in)}↓` : '—'}</span>
      <Tag tone={check === 'mismatch' ? 'error' : check === 'verified' ? 'done' : undefined}>
        {check}</Tag>
    </div>
  )
}
