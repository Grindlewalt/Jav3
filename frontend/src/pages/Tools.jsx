import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { notify, notifyError } from '../notify.js'
import Page from '../components/Page.jsx'
import Button from '../components/Button.jsx'
import Input from '../components/Input.jsx'
import Tag, { Badge } from '../components/Tag.jsx'
import Toggle from '../components/Toggle.jsx'
import EmptyState from '../components/EmptyState.jsx'

// Three groups (GET /api/tools `group`): the operator's own skills, open;
// imported OpenClaw skills, each a card with its requirements and a grant
// switch; and the built-in tool folders, folded shut and listed by name —
// the defaults exist, but they are not what this page is for.

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

export default function Tools() {
  const [tools, setTools] = useState(null)
  const [importing, setImporting] = useState(false)

  const load = useCallback(() => {
    api('/api/tools').then((r) => setTools(r.tools)).catch(notifyError)
  }, [])
  useEffect(load, [load])

  const list = tools || []
  const yours = list.filter((t) => t.group === 'yours')
  const imported = list.filter((t) => t.group === 'imported')
  const builtin = list.filter((t) => t.group === 'builtin')

  return (
    <Page title="Tools"
          actions={!importing && (
            <Button variant="ghost" onClick={() => setImporting(true)}>Import</Button>
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
        <details className="deleted-fold tools-fold">
          <summary>
            Built-in ({builtin.length})
            <span className="chev" aria-hidden="true">›</span>
          </summary>
          <ul className="builtin-list">
            {builtin.map((t) => (
              <li key={t.name} title={t.description}>
                <code>{t.name}</code>
                {t.enabled ? <Tag tone="done">on</Tag> : <Tag>off</Tag>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Page>
  )
}
