import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { notify, notifyError } from '../notify.js'
import Page from '../components/Page.jsx'
import Button from '../components/Button.jsx'
import Input from '../components/Input.jsx'
import Tag, { Badge } from '../components/Tag.jsx'
import Toggle from '../components/Toggle.jsx'
import EmptyState from '../components/EmptyState.jsx'
import { IMPORT_TIP, TOOLS_LEDE, builtinState } from '../securityCopy.js'
import { groupTools, heading, oneLine } from '../toolGroups.js'

// Three groups (GET /api/tools `group`): the operator's own skills, open;
// imported OpenClaw skills, each a card with its requirements and a grant
// switch; and the built-in tools, one card per tool AS THE MODEL SEES IT
// (toolGroups.js): a tool with several actions is one card listing them, and
// each action is still one tools/<name>/ folder, shown with its folder name.

const BUILTIN_HELP = 'Each card is one tool as the model sees it. A tool with several actions is '
  + 'called as tool(action=…); every action is a tool folder and its old name is shown beside it '
  + '(search finds either). A turn lists only the core tools until the model loads a section. '
  + 'The whole old-name map is docs/tool-map.md.'

const REQ_LABEL = {
  os: (r) => r.name,
  program: (r) => r.name,
  program_any: (r) => r.name,
  egress: () => 'egress',
  secret: (r) => `secret ${r.name}`,
  config: (r) => `config ${r.name}`,
}

function lockReason(t) {
  if (t.clash) return t.clash
  if (t.blocked) return t.blocked
  const unmet = (t.requirements || []).find((r) => !r.met)
  return unmet ? unmet.reason : ''
}

function ImportedCard({ t, onChanged }) {
  const [busy, setBusy] = useState(false)
  const reason = lockReason(t)
  // revoking is always allowed; granting only once every gate is green
  const locked = !t.granted && !!reason

  async function grant(on) {
    setBusy(true)
    try {
      await api(`/api/skills/${encodeURIComponent(t.slug)}/grant`, {
        method: 'PUT', body: JSON.stringify({ granted: on }),
      })
      notify(on ? `Granted ${t.name}` : `Revoked ${t.name}`)
      onChanged()
    } catch (e) { notifyError(e) } finally { setBusy(false) }
  }

  const ref = t.pin?.ref ? String(t.pin.ref).slice(0, 12) : ''
  return (
    <article className="tool-card">
      <div className="tool-card-head">
        <code>{t.name}</code>
        <Tag tone="untrusted">untrusted</Tag>
        <Toggle checked={!!t.granted} disabled={locked || busy}
                label={`Grant ${t.name}`} onChange={grant} />
      </div>
      <p>{t.description}</p>
      {(t.requirements || []).length > 0 && (
        <div className="tool-reqs">
          {t.requirements.map((r) => (
            <Tag key={`${r.kind}:${r.name}`} tone={r.met ? 'done' : 'error'}
                 title={r.reason || 'met'}>
              {REQ_LABEL[r.kind]?.(r) ?? r.name}
            </Tag>
          ))}
        </div>
      )}
      {reason && <div className="tool-meta tool-lock">{reason}</div>}
      {/* one line: a URL broken anywhere read as "skills/weat / her" */}
      <div className="tool-meta tool-src"
           title={[t.pin?.source, t.pin?.ref].filter(Boolean).join(' · ') || undefined}>
        {t.pin?.source}{ref && ` · ${ref}`}
      </div>
      <details className="output-fold tool-text">
        <summary>View text</summary>
        <pre>{t.body}</pre>
      </details>
    </article>
  )
}

function ImportBar({ onDone, onClose }) {
  const [source, setSource] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    try {
      const r = await api('/api/skills/import', {
        method: 'POST', body: JSON.stringify({ source: source.trim() }),
      })
      const flags = r.flags?.length ? ` · ${r.flags.length} flagged` : ''
      notify(`Imported ${r.name} — not granted${flags}`,
             r.flags?.length ? { sev: 'warn' } : {})
      onDone()
    } catch (e) {
      notifyError(e)
      setConfirming(false)
    } finally { setBusy(false) }
  }

  return (
    <div className="tool-import">
      {confirming ? (
        <>
          <span className="grow tool-import-ask">
            Import <code>{source.trim()}</code>? It lands ungranted.
          </span>
          <Button onClick={run} disabled={busy}>{busy ? 'Importing…' : 'Import'}</Button>
          <Button variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>
            Back
          </Button>
        </>
      ) : (
        <>
          <Input autoFocus value={source} aria-label="Skill folder or git URL"
                 placeholder="folder path or https git URL#subdir"
                 onChange={(e) => setSource(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === 'Enter' && source.trim()) setConfirming(true)
                   if (e.key === 'Escape') onClose()
                 }} />
          <Button onClick={() => setConfirming(true)} disabled={!source.trim()}>
            Next
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </>
      )}
    </div>
  )
}

function GateTags({ labels }) {
  return labels.map((g) => <Tag key={g} className="tool-gate" title="when the model is offered it">{g}</Tag>)
}

// One action of a merged tool (or the one action of a standalone tool): its
// name in the tool, the folder name it still answers to, what it does, whether
// the model is offered it now and why not.
function ActionRow({ row, gating, shared }) {
  const st = builtinState(row)
  const more = row.description && row.description !== oneLine(row.description)
  return (
    <li className="tool-action">
      <div className="tool-action-head">
        <code>{row.action}</code>
        <span className="dim small tool-old" title="the tool folder: the old name, and the name a transcript shows">
          {row.name}</span>
        <span className="grow" />
        <GateTags labels={gating} />
        {row.internal && <Tag title="the harness uses it; the model is not offered it on a normal turn">internal</Tag>}
        <Tag tone={st.tone || undefined}>{st.word}</Tag>
      </div>
      <div className="dim small builtin-desc">{oneLine(row.description)}</div>
      {row.reason && row.reason !== shared && <div className="small builtin-why">{row.reason}</div>}
      {(more || row.when_to_use) && (
        <details className="output-fold tool-text">
          <summary>Docs</summary>
          {more && <p className="small">{row.description}</p>}
          {row.when_to_use && <p className="small"><b>Use when:</b> {row.when_to_use}</p>}
        </details>
      )}
    </li>
  )
}

function ToolCard({ t }) {
  if (t.merged) {
    const n = t.actions.length
    return (
      <article className="tool-card tool-merged">
        <div className="tool-card-head">
          <code>{t.name}</code>
          {t.core && <Tag title="in every turn's tool list">core</Tag>}
          <GateTags labels={t.gating} />
          <Tag tone={t.offered === n ? 'done' : t.offered ? 'pending' : undefined}>
            {t.offered === n ? `${n} on` : `${t.offered} of ${n} on`}</Tag>
        </div>
        {t.about && <p>{t.about}</p>}
        {t.reason && <div className="small builtin-why">{t.reason}</div>}
        <ul className="tool-actions">
          {t.actions.map((a) => (
            <ActionRow key={a.row.name} row={a.row} gating={a.gating} shared={t.reason} />
          ))}
        </ul>
      </article>
    )
  }
  const row = t.actions[0].row
  const st = builtinState(row)
  const more = row.description && row.description !== t.about
  return (
    <article className="tool-card">
      <div className="tool-card-head">
        <code>{t.name}</code>
        {t.core && <Tag title="in every turn's tool list">core</Tag>}
        {row.internal && <Tag title="the harness uses it; the model is not offered it on a normal turn">internal</Tag>}
        <Tag tone={st.tone || undefined}>{st.word}</Tag>
      </div>
      {t.gating.length > 0 && <div className="tool-reqs"><GateTags labels={t.gating} /></div>}
      <p>{t.about}</p>
      {row.reason && <div className="small builtin-why">{row.reason}</div>}
      {(more || row.when_to_use) && (
        <details className="output-fold tool-text">
          <summary>Docs</summary>
          {more && <p className="small">{row.description}</p>}
          {row.when_to_use && <p className="small"><b>Use when:</b> {row.when_to_use}</p>}
        </details>
      )}
    </article>
  )
}

export function BuiltinTools({ rows, sections }) {
  const [query, setQuery] = useState('')
  const [internal, setInternal] = useState(false)
  const g = groupTools(rows, sections, { internal, query })
  const searching = query.trim() !== ''
  return (
    <>
      <h2 className="section-h">
        Built-in · {heading(g)}
        {searching && <span className="dim"> · {g.shownActions} match</span>}
      </h2>
      <p className="dim small tools-help">{BUILTIN_HELP}</p>
      <div className="tools-bar">
        <Input value={query} aria-label="Search built-in tools"
               placeholder="search by tool, action or old name"
               onChange={(e) => setQuery(e.target.value)} />
        {(internal || g.internalActions > 0) && (
          <Toggle checked={internal} onChange={setInternal} label="Show internal tools"
                  onText="Internal tools shown"
                  offText={`Show internal (${g.internalActions})`} />
        )}
      </div>
      {g.sections.length === 0 && <EmptyState>No built-in tool matches</EmptyState>}
      {g.sections.map((sec) => (
        <section key={sec.name}>
          <h3 className="tools-sec">{sec.name}
            {sec.about && <span className="dim small"> — {sec.about}</span>}</h3>
          <div className="tool-grid">
            {sec.tools.map((t) => <ToolCard key={t.name} t={t} />)}
          </div>
        </section>
      ))}
    </>
  )
}

export default function Tools() {
  const [tools, setTools] = useState(null)
  const [sections, setSections] = useState([])
  const [importing, setImporting] = useState(false)

  const load = useCallback(() => {
    api('/api/tools').then((r) => {
      setTools(r.tools)
      setSections(r.sections || [])
    }).catch(notifyError)
  }, [])
  useEffect(load, [load])

  const list = tools || []
  const yours = list.filter((t) => t.group === 'yours')
  const imported = list.filter((t) => t.group === 'imported')
  const builtin = list.filter((t) => t.group === 'builtin')

  return (
    <Page title="Tools" lede={TOOLS_LEDE}
          actions={!importing && (
            <Button variant="ghost" title={IMPORT_TIP} onClick={() => setImporting(true)}>
              Import a skill</Button>
          )}>
      {importing && (
        <ImportBar onClose={() => setImporting(false)}
                   onDone={() => { setImporting(false); load() }} />
      )}

      <h2 className="section-h tools-first">Your tools</h2>
      {tools && yours.length === 0
        ? <EmptyState>No skills of your own yet</EmptyState>
        : (
          <div className="tool-grid">
            {yours.map((t) => (
              <article key={t.name} className="tool-card">
                <div className="tool-card-head">
                  <code>{t.name}</code>
                  {t.clash
                    ? <Tag tone="error" title={t.clash}>clash</Tag>
                    : t.enabled ? <Badge>granted</Badge> : <Tag>off</Tag>}
                </div>
                <p>{t.description}</p>
              </article>
            ))}
          </div>
        )}

      {imported.length > 0 && (
        <>
          <h2 className="section-h">Imported ({imported.length})</h2>
          <div className="tool-grid">
            {imported.map((t) => <ImportedCard key={t.slug} t={t} onChanged={load} />)}
          </div>
        </>
      )}

      {builtin.length > 0 && (
        <BuiltinTools rows={builtin} sections={sections} />
      )}
    </Page>
  )
}
