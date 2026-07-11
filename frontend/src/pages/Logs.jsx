import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Md from '../Md.jsx'

// Logs: full transcript viewer for any conversation — every user/assistant
// message and every tool call with its args and result — plus the numbers that
// explain a token blow-up (tool-call counts, result bytes, real token usage).
// A debugging / observability tool. Tool args and results are UNTRUSTED: they
// are always rendered as plain text in <pre>, never markdown / HTML.

const RESULT_HOT = 4000       // a single result this big is re-sent every iteration
const HEAVY_TOKENS = 500000   // runaway-conversation flags in the left rail
const HEAVY_CALLS = 30

// bytes -> B / KB / MB
function human(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / (1024 * 1024)).toFixed(1)} MB`
}

// token counts -> K / M
function tok(n) {
  const v = Number(n) || 0
  if (v < 1000) return `${v}`
  if (v < 1000000) return `${(v / 1000).toFixed(1)}K`
  return `${(v / 1000000).toFixed(2)}M`
}

function prettyArgs(args) {
  if (args == null) return ''
  if (typeof args === 'object') {
    try { return JSON.stringify(args, null, 2) } catch { return String(args) }
  }
  const s = String(args)
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { return s }
}

function Tile({ label, value, sub, bad }) {
  return (
    <div className={`sbx-tile${bad ? ' bad' : ''}`}>
      <div className="n">{value}</div>
      <div className="dim small">{label}{sub ? ` · ${sub}` : ''}</div>
    </div>
  )
}

function usd(n) {
  const v = Number(n) || 0
  return v >= 0.01 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`
}

// One captured model call: token bill up front, raw context on demand.
// Context text is UNTRUSTED (it embeds tool results / web content) — always
// rendered inside <pre>, never through <Md>.
function CallItem({ call, index, prevInput }) {
  const [open, setOpen] = useState(false)
  const [ctx, setCtx] = useState(null)
  const [err, setErr] = useState(null)
  const delta = prevInput != null ? call.input_tokens - prevInput : null

  async function toggle() {
    if (open) { setOpen(false); return }
    setOpen(true)
    if (!ctx && !err && call.has_context) {
      try { setCtx(await api(`/api/logs/calls/${call.id}/context`)) }
      catch (e) { setErr(e.detail || String(e)) }
    }
  }
  return (
    <div className={`log-tool${open ? ' open' : ''}`}>
      <div className="log-tool-head" onClick={toggle}>
        <span className="dim">{open ? '▾' : '▸'}</span>
        <span className="mono log-tool-name">turn {index + 1}</span>
        <span className="dim small">
          in {tok(call.input_tokens)}
          {delta != null && delta !== 0 && ` (${delta > 0 ? '+' : ''}${tok(delta)})`}
          {' · '}hit {tok(call.cache_hit)} / miss {tok(call.cache_miss)}
          {' · '}out {tok(call.output_tokens)}
        </span>
        <span className="grow" />
        <span className="mono small">{usd(call.cost_usd)}</span>
        <span className="dim small">{call.created_at}</span>
      </div>
      {open && (
        <div className="log-tool-body">
          {!call.has_context && (
            <div className="dim small">no raw context stored for this call —
              flip “capture raw context” on the Cost tab before the run</div>
          )}
          {err && <div className="dim small">{err}</div>}
          {ctx && (
            <>
              <div className="dim small log-tool-label">
                {ctx.messages.length} messages · {ctx.n_tools} tool schemas
                attached (schemas count toward input tokens but are not shown)
              </div>
              {ctx.messages.map((m, i) => {
                const text = typeof m.content === 'string'
                  ? m.content : JSON.stringify(m.content)
                const body = m.tool_calls
                  ? `${text || ''}\n[tool_calls] ${JSON.stringify(m.tool_calls)}`
                  : text || ''
                return (
                  <div key={i}>
                    <div className="dim small log-tool-label">
                      {i + 1}. {m.role}{m.name ? ` (${m.name})` : ''}
                      {' · '}{human(body.length)}
                    </div>
                    <pre className="log-pre log-result">
                      {body.length > 20000
                        ? `${body.slice(0, 20000)}\n…(truncated for display)` : body}
                    </pre>
                  </div>
                )
              })}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function CostView() {
  const [data, setData] = useState(null)
  const load = () => api('/api/logs/costs').then(setData).catch(() => {})
  useEffect(() => { load() }, [])
  if (!data) return <div className="dim center-pad">…</div>
  const p = data.prices_per_m
  const order = ['24h', '7d', '30d', 'all']
  return (
    <div className="log-detail">
      <div className="sbx-card">
        <div className="sbx-verdict-top">
          <span className="tag">cost</span>
          <span className="grow" />
          <label className="dim small" style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={data.capture_context}
                   onChange={async (e) => {
                     await api('/api/logs/capture-context', {
                       method: 'POST',
                       body: JSON.stringify({ enabled: e.target.checked }) })
                     load()
                   }} />
            {' '}capture raw context per model call (heavy; kept a few days)
          </label>
        </div>
        <div className="sbx-tiles">
          {order.map((w) => (
            <Tile key={w} label={w === 'all' ? 'all time' : `last ${w}`}
                  value={usd(data.windows[w]?.cost_usd)}
                  sub={`${data.windows[w]?.calls || 0} calls`} />
          ))}
        </div>
      </div>
      {order.map((w) => {
        const d = data.windows[w]
        if (!d) return null
        return (
          <section key={w} className="sbx-sec">
            <div className="sbx-sec-head">
              <h3>{w === 'all' ? 'All time' : `Last ${w}`}</h3>
              <span className="dim small">{d.calls} model calls</span>
            </div>
            <div className="log-hist">
              <div className="log-hist-row">
                <span className="mono log-hist-name">cache hit</span>
                <span className="dim small grow">{tok(d.cache_hit)} tok × ${p.cache_hit}/M</span>
                <span className="mono small">{usd(d.cache_hit * p.cache_hit / 1e6)}</span>
              </div>
              <div className="log-hist-row">
                <span className="mono log-hist-name">cache miss</span>
                <span className="dim small grow">{tok(d.cache_miss)} tok × ${p.cache_miss}/M</span>
                <span className="mono small">{usd(d.cache_miss * p.cache_miss / 1e6)}</span>
              </div>
              <div className="log-hist-row">
                <span className="mono log-hist-name">output</span>
                <span className="dim small grow">{tok(d.output)} tok × ${p.output}/M</span>
                <span className="mono small">{usd(d.output * p.output / 1e6)}</span>
              </div>
              <div className="log-hist-row">
                <span className="mono log-hist-name">total</span>
                <span className="dim small grow" />
                <span className="mono small">{usd(d.cost_usd)}</span>
              </div>
            </div>
          </section>
        )
      })}
    </div>
  )
}

function ToolItem({ item }) {
  const [open, setOpen] = useState(false)
  const hot = (item.result_bytes || 0) > RESULT_HOT
  const result = String(item.result ?? '')
  const truncated = result.length > 10000
  const shown = truncated ? `${result.slice(0, 10000)}\n…(truncated)` : result
  return (
    <div className={`log-tool${open ? ' open' : ''}`}>
      <div className="log-tool-head" onClick={() => setOpen((o) => !o)}>
        <span className="dim">{open ? '▾' : '▸'}</span>
        <span className="mono log-tool-name">{item.tool}</span>
        <span className={`log-size${hot ? ' hot' : ''}`}>{human(item.result_bytes)}</span>
        <span className="grow" />
        <span className="dim small">{item.ts}</span>
      </div>
      {open && (
        <div className="log-tool-body">
          <div className="dim small log-tool-label">args</div>
          <pre className="log-pre">{prettyArgs(item.args)}</pre>
          <div className="dim small log-tool-label">result</div>
          <pre className="log-pre log-result">{shown}</pre>
        </div>
      )}
    </div>
  )
}

export default function Logs() {
  const [convos, setConvos] = useState([])
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [calls, setCalls] = useState([])
  const [view, setView] = useState('logs')   // 'logs' | 'cost'
  const selectedRef = useRef(null)

  const refresh = () =>
    api('/api/logs/conversations').then((r) => setConvos(r.conversations)).catch(() => {})

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [])

  function open(id) {
    selectedRef.current = id
    setSelected(id); setDetail(null); setCalls([])
    api(`/api/logs/conversations/${id}`)
      .then((d) => { if (selectedRef.current === id) setDetail(d) })
      .catch(() => {})
    api(`/api/logs/conversations/${id}/calls`)
      .then((r) => { if (selectedRef.current === id) setCalls(r.calls) })
      .catch(() => {})
  }

  const stats = detail?.stats || {}
  const hist = detail?.tool_histogram || []
  const maxBytes = hist.reduce((m, h) => Math.max(m, h.bytes || 0), 0) || 1
  const cacheTotal = (stats.cache_hit || 0) + (stats.cache_miss || 0)
  const cachePct = cacheTotal ? Math.round((stats.cache_hit / cacheTotal) * 100) : null

  const totalCost = calls.reduce((s, c) => s + (c.cost_usd || 0), 0)

  return (
    <div className="split-layout">
      <aside>
        <div className="side-title">Logs</div>
        <div className="row" style={{ gap: 6, marginBottom: 8 }}>
          <button className={view === 'logs' ? '' : 'ghost'}
                  onClick={() => setView('logs')}>transcripts</button>
          <button className={view === 'cost' ? '' : 'ghost'}
                  onClick={() => setView('cost')}>cost</button>
        </div>
        <ul className="file-list">
          {convos.map((c) => {
            const heavyTok = (c.input_tokens || 0) > HEAVY_TOKENS
            const heavyCalls = (c.tool_calls || 0) > HEAVY_CALLS
            return (
              <li key={c.id} className={`log-row${selected === c.id ? ' active' : ''}`}
                  onClick={() => open(c.id)}>
                <div className="log-row-top">
                  <span className="grow ellipsis" title={c.summary || `#${c.id}`}>
                    {c.summary || `#${c.id}`}</span>
                  <span className="tag">{c.kind}</span>
                </div>
                <div className="log-row-meta">
                  <span className={heavyCalls ? 'log-heat' : ''}>{c.tool_calls || 0} calls</span>
                  <span className="dim"> · </span>
                  <span>{human(c.result_bytes)}</span>
                  {(c.input_tokens || 0) > 0 && (
                    <>
                      <span className="dim"> · </span>
                      <span className={heavyTok ? 'log-heat' : ''}>{tok(c.input_tokens)} tok</span>
                    </>
                  )}
                  {c.project && <span className="tag">{c.project}</span>}
                </div>
              </li>
            )
          })}
          {convos.length === 0 && (
            <li className="dim" style={{ cursor: 'default' }}>no conversations yet</li>
          )}
        </ul>
      </aside>

      <main className="editor-pane">
        {view === 'cost' ? (
          <CostView />
        ) : !detail ? (
          <div className="dim center-pad">pick a conversation to read its full transcript</div>
        ) : (
          <div className="log-detail">
            <div className="sbx-card">
              <div className="sbx-verdict-top">
                <span className="tag">{detail.kind}</span>
                <span className="mono ellipsis grow" title={detail.summary || `#${detail.id}`}>
                  {detail.summary || `#${detail.id}`}</span>
              </div>
              <div className="sbx-tiles">
                <Tile label="input tokens" value={tok(stats.input_tokens)}
                      bad={(stats.input_tokens || 0) > HEAVY_TOKENS} />
                <Tile label="output tokens" value={tok(stats.output_tokens)} />
                <Tile label="tool calls" value={stats.tool_calls || 0}
                      bad={(stats.tool_calls || 0) > HEAVY_CALLS} />
                <Tile label="result bytes" value={human(stats.result_bytes)} />
                <Tile label="turns" value={stats.turns || 0} />
                <Tile label="cache hit" value={cachePct == null ? '—' : `${cachePct}%`}
                      sub={cacheTotal ? `${stats.cache_hit}/${cacheTotal}` : null} />
                {calls.length > 0 && <Tile label="cost" value={usd(totalCost)}
                      sub={`${calls.length} calls`} />}
              </div>
            </div>

            {calls.length > 0 && (
              <section className="sbx-sec">
                <div className="sbx-sec-head">
                  <h3>Model calls</h3>
                  <span className="dim small">
                    the exact context sent per API call — capture toggles on the cost tab</span>
                </div>
                <div className="log-timeline">
                  {calls.map((c, i) => (
                    <CallItem key={c.id} call={c} index={i}
                              prevInput={i > 0 ? calls[i - 1].input_tokens : null} />
                  ))}
                </div>
              </section>
            )}

            {hist.length > 0 && (
              <section className="sbx-sec">
                <div className="sbx-sec-head">
                  <h3>Tool histogram</h3>
                  <span className="dim small">by total result bytes</span>
                </div>
                <div className="log-hist">
                  {hist.map((h) => (
                    <div key={h.tool} className="log-hist-row">
                      <span className="mono log-hist-name ellipsis" title={h.tool}>{h.tool}</span>
                      <span className="dim small log-hist-count">×{h.count}</span>
                      <div className="log-hist-track">
                        <div className="log-hist-bar"
                             style={{ width: `${((h.bytes || 0) / maxBytes) * 100}%` }} />
                      </div>
                      <span className="dim small log-hist-bytes">{human(h.bytes)}</span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <section className="sbx-sec">
              <div className="sbx-sec-head">
                <h3>Transcript</h3>
                <span className="dim small">{(detail.timeline || []).length} items · in order</span>
              </div>
              <div className="log-timeline">
                {(detail.timeline || []).map((item, i) => (
                  item.kind === 'tool' ? (
                    <ToolItem key={i} item={item} />
                  ) : (
                    <div key={i} className={`log-msg ${item.role}`}>
                      <div className="log-msg-head">
                        <span className="log-role">{item.role}</span>
                        {item.ts && <span className="dim small">{item.ts}</span>}
                      </div>
                      <div className="log-msg-body"><Md text={item.content} /></div>
                    </div>
                  )
                ))}
                {(detail.timeline || []).length === 0 && (
                  <div className="dim center-pad">no transcript</div>
                )}
              </div>
            </section>
          </div>
        )}
      </main>
    </div>
  )
}
