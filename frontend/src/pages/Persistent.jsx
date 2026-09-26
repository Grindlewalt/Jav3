import { useEffect, useMemo, useState } from 'react'
import { Button, EmptyState, Modal, Tag, Toggle } from '../components/index.js'
import { notify, notifyError } from '../notify.js'
import { ago } from '../format.js'
import { followProcs, listProcesses } from '../boxes/api/procs.js'
import {
  listServices, revokeService, serviceLogs, startService, stopService,
} from '../boxes/api/services.js'
import {
  SERVICE_STATE_TONE, boxIsOdd, boxTotals, bytes, connCheck, flattenTree, mergeProcs, normBoxProcs,
  procsRefetch, sortBoxes,
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
  const { data, error, unavailable, setData, reload } = useLoad(
    () => listProcesses().then((r) => ({ enabled: r.enabled, boxes: sortBoxes(r.boxes.map(normBoxProcs)) })))
  const svc = useLoad(() => listServices(), { every: 20000 })
  const [onlyOdd, setOnlyOdd] = useState(false)
  const [dlg, setDlg] = useState(null)
  const [logs, setLogs] = useState(null)

  // One box per event: fold box_procs / box_gone in place; fetch what a
  // box_procs_changed (row too big for the stream) or a (re)opened stream asks.
  useEffect(() => followProcs((ev) => {
    setData((prev) => {
      if (!prev) return prev
      const boxes = mergeProcs(prev.boxes, ev)
      return boxes === prev.boxes ? prev : { ...prev, boxes }
    })
    const want = procsRefetch(ev)
    if (want === '*') reload()
    else if (want) {
      listProcesses(want).then((r) => {
        const row = r.boxes.find((b) => b.box_id === want)
        setData((prev) => (prev ? { ...prev, boxes: mergeProcs(prev.boxes,
          row ? { type: 'box_procs', box: row } : { type: 'box_gone', box_id: want }) } : prev))
      }).catch((e) => {
        if (e?.status === 404) setData((prev) => (prev ? { ...prev,
          boxes: mergeProcs(prev.boxes, { type: 'box_gone', box_id: want }) } : prev))
      })
    }
  }), [setData, reload])

  const services = useMemo(() => Object.fromEntries(
    (svc.data?.services || []).map((s) => [s.id, s])), [svc.data])

  async function stop(s) {
    try { await stopService(s.id); svc.reload() } catch (e) { notifyError(e) }
  }
  async function start(s) {
    try { await startService(s.id); svc.reload() } catch (e) { notifyError(e) }
  }
  async function revoke(s, del) {
    try {
      const r = await revokeService(s.id, del)
      if (r?.data_deleted) notify(`service ${s.name}: revoked, /srv data deleted`)
      setDlg(null); svc.reload()
    } catch (e) { notifyError(e) }
  }

  if (unavailable) return <div className="bx-page"><Unavailable what="The process view" /></div>
  const boxes = data?.boxes || []
  const all = boxes.reduce((t, b) => {
    const x = boxTotals(b)
    return { procs: t.procs + x.procs, unexpected: t.unexpected + x.unexpected,
      mism: t.mism + x.mismatches, orphans: t.orphans + b.orphan_conns.length }
  }, { procs: 0, unexpected: 0, mism: 0, orphans: 0 })

  return (
    <div className="bx-page">
      <LoadError error={error} />
      {data && !data.enabled && (
        <div className="bx-unavail dim small">The process view is switched off on the server
          (boxes are off): no box reports its processes.</div>
      )}
      <div className="net-top">
        <div className="net-counts">
          <span><b>{boxes.length}</b> boxes</span>
          <span><b>{all.procs}</b> processes</span>
          <span className={all.unexpected ? 'bx-red' : ''}><b>{all.unexpected}</b> unexpected</span>
          <span className={all.mism ? 'bx-red' : ''}><b>{all.mism}</b> byte mismatches</span>
          {all.orphans > 0 && (
            <span className="bx-red"><b>{all.orphans}</b> unowned connection{all.orphans === 1 ? '' : 's'}</span>)}
        </div>
        <span className="grow" />
        <Toggle checked={onlyOdd} onChange={setOnlyOdd} label="only boxes with something unexpected"
                onText="only unexpected" offText="every box" />
      </div>
      <ServiceList services={svc.data?.services} relays={svc.data?.relays} onStop={stop}
                   onStart={start} onRevoke={(x) => setDlg(x)} onLogs={setLogs} />
      <ServiceLogs s={logs} onClose={() => setLogs(null)} />
      {!data && !error && <div className="dim">…</div>}
      {data && data.enabled && boxes.length === 0 && <EmptyState pad>no box is reporting</EmptyState>}
      {boxes.filter((b) => !onlyOdd || boxIsOdd(b))
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
// approved with (decision 0.1: shown wherever a service is listed), what the
// box last reported (or "unreported"), the error it gave, and each exposed
// port's relay. Start/Stop follow desired_state: that is what the host acts on.
function ServiceList({ services, relays, onStop, onStart, onRevoke, onLogs }) {
  const approved = (services || []).filter((s) => s.status === 'approved')
  if (!approved.length) return null
  return (
    <section className="sbx-sec">
      <div className="sbx-sec-head"><h3>Services</h3>
        <span className="sec-count">{approved.length}</span></div>
      <ul className="staged-list rev-list">
        {approved.map((s) => {
          const want = s.desired_state === 'running'
          const rel = (relays || []).filter((r) => r.service_id === s.id)
          return (
            <li key={s.id} className="bx-svc-row">
              <span className={`run-dot ${s.state === 'running' ? 'done' : s.state === 'failed' ? 'error' : ''}`}
                    aria-hidden="true" />
              <b className="mono">{s.name}</b>
              <span className="dim small">#{s.id} · {s.project_slug}{s.box_id ? ` · ${s.box_id}` : ''}</span>
              <PlacementTag placement={s.placement} />
              <Tag tone={SERVICE_STATE_TONE[s.state]}
                   title={s.last_reported_at ? `last reported ${s.last_reported_at}` : 'never reported'}>
                {s.state || 'unreported'}</Tag>
              {!want && s.state === 'running' && <Tag tone="pending">stopping</Tag>}
              {(s.expose_ports || []).map((p) => {
                const r = rel.find((x) => x.port === p.port)
                const bad = r && (r.error || !r.listening)
                return (
                  <Tag key={p.port} tone={bad ? 'error' : undefined}
                       title={r ? `${r.address || ''} · ${r.conns} conn(s) · ${bytes(r.bytes_out)}↑ ${bytes(r.bytes_in)}↓${r.error ? ` · ${r.error}` : ''}`
                         : 'no relay open'}>
                    {p.port} → {p.bind === 'lan' ? 'LAN' : 'loopback'}</Tag>
                )
              })}
              <span className="grow" />
              <Button variant="ghost" onClick={() => onLogs(s)}>Logs</Button>
              {want
                ? <Button variant="ghost" onClick={() => onStop(s)}>Stop</Button>
                : <Button variant="ghost" onClick={() => onStart(s)}>Start</Button>}
              <Button variant="ghost" danger onClick={() => onRevoke(s)}>Revoke</Button>
              {s.error && <div className="error small bx-svc-err">{s.error}</div>}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

// The service's journal from its box. Guest-written: one text node in a <pre>.
function ServiceLogs({ s, onClose }) {
  const [text, setText] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    if (!s) return
    setText(null); setErr(null)
    serviceLogs(s.id, 300).then((r) => setText(r.text)).catch(setErr)
  }, [s])
  if (!s) return null
  return (
    <Modal open title={`Logs: ${s.name} (#${s.id})`} onClose={onClose} width={760}
           footer={<Button variant="ghost" onClick={onClose}>Close</Button>}>
      <div className="dim small">written by the guest — untrusted</div>
      {err && <div className="error small">{err.detail || String(err)}</div>}
      {text == null && !err && <div className="dim">…</div>}
      {text != null && <pre className="mono small bx-logs">{text || '(empty)'}</pre>}
    </Modal>
  )
}

function BoxTree({ b, services, onStop, onRevoke }) {
  const [collapsed, setCollapsed] = useState(() => new Set())
  const t = useMemo(() => boxTotals(b), [b])
  const rows = useMemo(() => flattenTree(b.tree, collapsed), [b.tree, collapsed])
  const flip = (key) => setCollapsed((c) => {
    const n = new Set(c)
    if (n.has(key)) n.delete(key); else n.add(key)
    return n
  })
  return (
    <details className={`sbx-card bx-tree${boxIsOdd(b) ? ' odd' : ''}`} open>
      <summary className="bx-tree-head">
        <b className="mono">{b.box_id}</b>
        <span className="dim small">{b.kind}{b.project ? ` · ${b.project}` : ''}</span>
        {b.stale && <Tag tone="pending" title="the box has not reported recently">stale</Tag>}
        {b.truncated && <Tag tone="pending" title="the box reported more than fits; totals count everything">truncated</Tag>}
        {b.baseline === 'builtin' && (
          <Tag title="no image baseline yet: 'unexpected' is judged against the built-in set">built-in baseline</Tag>)}
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
      {b.error && <div className="error small bx-row-err">{b.error}</div>}
      {b.orphan_conns.length > 0 && (
        <div className="bx-orphans">
          <div className="small bx-red"><b>Possibly hidden process</b> — the host saw
            {' '}{b.orphan_conns.length} connection{b.orphan_conns.length === 1 ? '' : 's'} from this box
            that no reported process owns.</div>
          {b.orphan_conns.map((c, i) => <ConnRow key={i} c={c} depth={0} orphan />)}
        </div>
      )}
      {rows.length === 0 && !b.error && <div className="dim small bx-none">nothing beyond the OS baseline</div>}
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
            <Button variant="ghost" disabled={svc.desired_state !== 'running'} onClick={() => onStop(svc)}>Stop</Button>
            <Button variant="ghost" danger onClick={() => onRevoke(svc)}>Revoke</Button>
          </>}
        </span>
      </div>
      {(n.conns || []).map((c, i) => <ConnRow key={i} c={c} depth={r.depth} />)}
    </>
  )
}

function ConnRow({ c, depth, orphan = false }) {
  const check = orphan ? 'unowned' : connCheck(c)
  const peer = c.host || (c.raddr ? `${c.raddr}:${c.rport}` : '—')
  return (
    <div className={`bx-conn${check === 'mismatch' || orphan ? ' mismatch' : ''}`}
         style={{ paddingLeft: `${depth * 18 + 44}px` }}>
      <span className="mono small">{c.dir === 'in' ? '⇠ in ' : '⇢ out'} {c.proto}</span>
      <span className="mono small bx-cmd" title={`${c.laddr}:${c.lport} ${c.dir === 'in' ? '←' : '→'} ${peer}`}>
        :{c.lport} {c.dir === 'in' ? '←' : '→'} {peer}</span>
      <span className="dim small">{c.state}</span>
      <span className="small" title="guest-reported: sent / received">
        guest {bytes(c.guest_bytes_out)}↑ {bytes(c.guest_bytes_in)}↓</span>
      <span className="small" title="host-metered (proxy / relay): sent / received">
        host {c.host_bytes_out != null ? `${bytes(c.host_bytes_out)}↑ ${bytes(c.host_bytes_in)}↓` : '—'}</span>
      <Tag tone={check === 'mismatch' || orphan ? 'error' : check === 'verified' ? 'done' : undefined}>
        {check}</Tag>
    </div>
  )
}
