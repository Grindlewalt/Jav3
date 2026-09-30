/* Settings → Grounding: the model finder (backend/grounding.py,
 * grounding_api.py; docs/navigation-contract.md C).
 *
 * When the agent clicks a described target on a computer ("the Save button in
 * the dialog") and no accessibility element matches, a vision model has to
 * turn the words into a point on the screenshot. Models differ a lot at that,
 * and in how they write coordinates (pixels, 0-1000, 0-1). "Find grounding
 * model" asks every enabled image-capable model to find the labelled targets
 * on 24 synthetic screens (light, dark, Retina, tiny icons, a duplicate "Save"
 * label, near-identical rows), learns each model's coordinate convention,
 * and ranks them by hit rate, then cost, then latency. The winner is used
 * automatically unless a model is pinned here. The probe costs a few cents
 * per model (the $/1k column is per thousand locates) and runs in the
 * background; the page polls only while it runs. */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { ago } from './format.js'
import { notifyError } from './notify.js'
import Button from './components/Button.jsx'
import Card from './components/Card.jsx'
import EmptyState from './components/EmptyState.jsx'
import Select from './components/Select.jsx'
import Tag from './components/Tag.jsx'

const POLL_MS = 2000

const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`)
const num = (v, d = 0) => (v == null ? '–' : Number(v).toFixed(d))
const money = (v) => (v == null ? '–' : `$${v < 1 ? v.toFixed(3) : v.toFixed(2)}`)
const CONV = { px: 'pixels', k1000: '0–1000', unit: '0–1' }

export default function GroundingPanel() {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => api('/api/grounding').then(setData)
    .catch(() => setData((d) => d || { ranking: [], candidates: [] })), [])

  useEffect(() => { load() }, [load])

  const running = !!data?.running
  useEffect(() => {
    if (!running) return undefined
    const id = setInterval(() => { if (!document.hidden) load() }, POLL_MS)
    return () => clearInterval(id)
  }, [running, load])

  const probe = async () => {
    setBusy(true)
    try { await api('/api/grounding/probe', { method: 'POST', body: '{}' }) }
    catch (e) { notifyError(e) }
    setBusy(false)
    load()
  }

  const cancel = async () => {
    try { await api('/api/grounding/probe/cancel', { method: 'POST' }) }
    catch (e) { notifyError(e) }
    load()
  }

  const pin = async (model) => {
    try {
      setData(await api('/api/grounding', { method: 'PUT', body: JSON.stringify({ model }) }))
    } catch (e) { notifyError(e); load() }
  }

  const cands = data?.candidates || []
  const job = data?.job
  const labels = Object.fromEntries(cands.map((c) => [c.id, c.label]))
  const pinnedByConfig = data?.pinned_by === 'config'

  return (
    <Card title="Grounding" headingLevel={2} id="grounding">
      <p className="dim small settings-note">
        Tests every enabled image-capable model on 24 labelled screens and picks the one
        that clicks most accurately for <code>desk_click target=…</code>.
      </p>
      {data === null ? <EmptyState>loading…</EmptyState>
        : cands.length === 0 && (data.ranking || []).length === 0 ? (
          <EmptyState>
            No image-capable model is enabled. Switch one on
            in <a href="#providers">API providers</a> (models marked vision).
          </EmptyState>
        ) : (
          <>
            <div className="settings-actions">
              {running ? (
                <>
                  <span className="small">
                    Testing… {job?.done ?? 0} / {job?.total || '?'}
                    {job?.current ? <span className="dim"> · {job.current}</span> : null}
                  </span>
                  <progress max={job?.total || 1} value={job?.done || 0}
                            aria-label="Model finder progress" />
                  <Button variant="ghost" onClick={cancel}>Cancel</Button>
                </>
              ) : (
                <Button onClick={probe} disabled={busy || cands.length === 0}>
                  Find grounding model</Button>
              )}
              {data.probed_at && !running && (
                <span className="dim small">measured {ago(data.probed_at)}</span>
              )}
            </div>
            {job?.error && !running && (
              <p className={`small ${job.cancelled ? 'dim' : 'warn'}`}>
                Last run: {job.error}</p>
            )}
            {(data.ranking || []).length > 0 && (
              <div className="sbd-tablewrap">
                <table className="sbd-table">
                  <thead>
                    <tr><th>Model</th><th>Hits</th><th>Median px</th><th>p95 ms</th>
                      <th>$/1k</th><th>Coords</th><th>Measured</th><th /></tr>
                  </thead>
                  <tbody>
                    {data.ranking.map((r) => (
                      <tr key={r.model}>
                        <td title={r.model}>
                          {labels[r.model] || r.model}
                          {r.model === data.model && <> <Tag tone="done">in use</Tag></>}
                        </td>
                        <td>{pct(r.hit_rate)} <span className="dim">of {r.n}</span></td>
                        <td>{num(r.median_px, 1)}</td>
                        <td>{num(r.p95_ms)}</td>
                        <td>{money(r.cost_per_1k)}</td>
                        <td>{CONV[r.convention] || r.convention}</td>
                        <td className="dim small" title={r.probed_at || ''}>
                          {r.probed_at ? ago(r.probed_at) : ''}</td>
                        <td>
                          {r.stale && <Tag tone="error">stale</Tag>}
                          {(r.conf_hit != null || r.conf_miss != null) && (
                            <span className="dim small"
                                  title="mean confidence the model reported on hits and on misses">
                              {' '}conf hit {num(r.conf_hit, 2)} / miss {num(r.conf_miss, 2)}</span>
                          )}
                          {r.unusable && <Tag tone="error">unusable</Tag>}
                          {r.errors > 0 && (
                            <span className="dim small" title={r.last_error || ''}>
                              {' '}{r.errors} error{r.errors === 1 ? '' : 's'}</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="settings-actions">
              <Select label="Grounding model" value={pinnedByConfig ? '' : (data.pinned || '')}
                      disabled={pinnedByConfig}
                      onChange={(e) => pin(e.target.value)}
                      hint={pinnedByConfig
                        ? `Set by JARVIS_GROUNDING_MODEL: ${data.pinned}`
                        : data.model ? `Now using ${labels[data.model] || data.model}`
                          : 'None yet: run the model finder or pick one'}
                      options={[{ value: '', label: 'Automatic (best measured)' },
                                ...cands.map((c) => ({ value: c.id, label: c.label }))]} />
            </div>
          </>
        )}
    </Card>
  )
}
