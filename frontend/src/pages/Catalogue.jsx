import { useMemo, useState } from 'react'
import { Button, EmptyState, Input, Select, Tag } from '../components/index.js'
import { listPackages } from '../boxes/api/packages.js'
import { listImages } from '../boxes/api/images.js'
import { filterCatalogue, PKG_STATUSES } from '../boxes/logic.js'
import { LoadError, Unavailable, useLoad } from '../boxes/ui.jsx'
import { PackageApprove, PackageSummary, usePackageReject } from '../boxes/RequestCards.jsx'

// Every package request, the agent's and the operator's: what was asked
// (and the command the agent wanted), the command the host will actually
// run, the resolved version and integrity, why, who asked, and where it
// stands. Pending rows are decided here or in the Security queue.

const STATUS_TONE = {
  pending: 'pending', approved: 'running', building: 'running', built: 'done',
  failed: 'error', rejected: 'error', removed: undefined,
}

export default function Catalogue() {
  const pk = useLoad(() => listPackages(), { every: 15000 })
  const im = useLoad(listImages)
  const [f, setF] = useState({ status: '', manager: '', q: '' })
  const [approving, setApproving] = useState(null)
  const reject = usePackageReject(pk.reload)

  const rows = useMemo(() => filterCatalogue(pk.data, f), [pk.data, f])
  const pending = (pk.data || []).filter((r) => r.status === 'pending').length

  if (pk.unavailable) return <div className="bx-page"><Unavailable what="The package catalogue" /></div>
  return (
    <div className="bx-page">
      <LoadError error={pk.error} />
      <div className="net-top">
        <Select aria-label="status" value={f.status} onChange={(e) => setF({ ...f, status: e.target.value })}
                options={[{ value: '', label: `All statuses${pending ? ` (${pending} pending)` : ''}` },
                  ...PKG_STATUSES]} />
        <Select aria-label="manager" value={f.manager} onChange={(e) => setF({ ...f, manager: e.target.value })}
                options={[{ value: '', label: 'Every manager' }, 'apt', 'pip', 'npm']} />
        <Input aria-label="search" placeholder="package, project, reason…" value={f.q}
               onChange={(e) => setF({ ...f, q: e.target.value })} />
      </div>
      {!pk.data && !pk.error && <div className="dim">…</div>}
      {pk.data && rows.length === 0 && <EmptyState>no package requests{f.status || f.manager || f.q ? ' match' : ''}</EmptyState>}
      <div className="bx-cat">
        {rows.map((p) => (
          <div key={p.id} className={`sbx-row bx-cat-row${p.status === 'pending' ? ' sev-warn' : ''}`}>
            <div className="grow">
              <PackageSummary p={p} />
            </div>
            <div className="sbx-right bx-cat-right">
              <Tag tone={STATUS_TONE[p.status]}>{p.status}</Tag>
              <span className="small">into <code>{p.target_variant}</code>
                {p.built_version ? <span className="dim"> ({p.built_version})</span> : null}</span>
              {p.status === 'pending' && (
                <span className="row">
                  <Button variant="ghost" onClick={() => setApproving(p)}>Approve…</Button>
                  <Button variant="ghost" danger onClick={() => reject(p)}>Reject</Button>
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
      <PackageApprove p={approving} variants={im.data?.variants} onClose={() => setApproving(null)}
                      onDone={() => { setApproving(null); pk.reload() }} />
    </div>
  )
}
