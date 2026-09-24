/* Settings → Backup: the rclone backup of the state dir (backend/backup.py).
 *
 * Status comes from GET /api/backup/status, the form from GET/PUT
 * /api/backup/config. Passwords are write-only: the API answers
 * `crypt_password_set`, never the value, so the field starts empty and an
 * empty field on save means "keep" (the PUT treats an omitted key that way).
 * Restore is deliberately not a button — it replaces the whole state dir and
 * refuses while the server runs on it, so it lives in the CLI. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { human, ts } from './format.js'
import Card from './components/Card.jsx'
import Button from './components/Button.jsx'
import Input from './components/Input.jsx'
import Tag from './components/Tag.jsx'
import Toggle from './components/Toggle.jsx'
import EmptyState from './components/EmptyState.jsx'

const POLL_MS = 3000
const POLL_MAX = 200             // ~10 minutes, then stop asking

// The switch's name and its visible caption: the same words in both states,
// since the switch itself shows on/off.
const SECRETS = 'Include secrets (encrypted)'

const errText =(e) => e?.detail || String(e)

export default function BackupPanel() {
  const [st, setSt] = useState(null)
  const [cfg, setCfg] = useState(null)
  const [form, setForm] = useState(null)
  const [pw, setPw] = useState('')
  const [saveErr, setSaveErr] = useState(null)
  const [runMsg, setRunMsg] = useState(null)    // {error?: bool, text}
  const [polling, setPolling] = useState(false)
  const ticks = useRef(0)

  const loadStatus = useCallback(() => api('/api/backup/status').then((s) => {
    setSt(s)
    return s
  }), [])

  useEffect(() => {
    loadStatus().then((s) => { if (s.running) setPolling(true) }).catch(() => {})
    api('/api/backup/config').then((c) => { setCfg(c); setForm(fromCfg(c)) })
      .catch(() => {})
  }, [loadStatus])

  useEffect(() => {
    if (!polling) return undefined
    ticks.current = 0
    const id = setInterval(() => {
      ticks.current += 1
      loadStatus().then((s) => {
        if (!s.running || ticks.current >= POLL_MAX) {
          setPolling(false)
          if (!s.running && s.last) {
            setRunMsg(s.last.ok ? { text: 'Backup finished.' }
              : { error: true, text: s.last.error || 'Backup failed.' })
          }
        }
      }).catch(() => { if (ticks.current >= POLL_MAX) setPolling(false) })
    }, POLL_MS)
    return () => clearInterval(id)
  }, [polling, loadStatus])

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }))

  async function save(e) {
    e.preventDefault()
    setSaveErr(null)
    const body = { remote: form.remote, include_secrets: form.include_secrets,
                   crypt_remote: form.crypt_remote }
    if (pw) body.crypt_password = pw
    try {
      const c = await api('/api/backup/config',
        { method: 'PUT', body: JSON.stringify(body) })
      setCfg(c); setForm(fromCfg(c)); setPw('')
      loadStatus().catch(() => {})
    } catch (err) { setSaveErr(errText(err)) }
  }

  async function runNow() {
    setRunMsg(null)
    try {
      await api('/api/backup/run', { method: 'POST' })
      setRunMsg({ text: 'Backing up…' })
      setSt((s) => (s ? { ...s, running: true } : s))
      setPolling(true)
    } catch (err) {
      if (err.status === 409) {
        setRunMsg({ text: 'A backup is already running.' })
        setPolling(true)
      } else setRunMsg({ error: true, text: errText(err) })
    }
  }

  const dirty = form && cfg && (pw !== '' || form.remote !== (cfg.remote || '')
    || form.include_secrets !== !!cfg.include_secrets
    || form.crypt_remote !== (cfg.crypt_remote || ''))
  const running = !!st?.running || polling

  return (
    <Card title="Backup" headingLevel={2}>
      <p className="dim small">
        Copies memory, projects, agents, skills and a snapshot of the database to
        an rclone remote, on a timer and on demand. Uses rclone (MIT).
      </p>

      {!st ? <p className="dim">loading…</p>
        : !st.rclone?.available ? (
          <EmptyState hint={st.rclone?.install_hint}>
            rclone is not installed on the server.
          </EmptyState>
        ) : <StatusRow st={st} running={running} />}

      {form && (
        <form onSubmit={save} className="stack">
          <Input label="Remote" placeholder="remote:path" value={form.remote}
                 hint="An rclone remote and path, as named in the server's rclone config."
                 onChange={(e) => set('remote', e.target.value)} />
          <div className="row">
            <Toggle checked={form.include_secrets} label={SECRETS}
                    onText={SECRETS} offText={SECRETS}
                    onChange={(v) => set('include_secrets', v)} />
          </div>
          {form.include_secrets && (
            <>
              <p className="dim small">
                Secrets only ever go up through an rclone crypt layer: name a crypt
                remote of your own, or set a password to have one built. With
                neither, saving is refused.
              </p>
              <Input label="Crypt remote" placeholder="crypt:" value={form.crypt_remote}
                     onChange={(e) => set('crypt_remote', e.target.value)} />
              <Input label="Crypt password" type="password" autoComplete="new-password"
                     value={pw}
                     placeholder={cfg.crypt_password_set ? 'set — type to replace'
                       : 'not set'}
                     hint={cfg.crypt_password_set ? 'A password is set.' : 'No password set.'}
                     onChange={(e) => setPw(e.target.value)} />
            </>
          )}
          {saveErr && <p className="error">{saveErr}</p>}
          <div className="row">
            <Button type="submit" disabled={!dirty}>{dirty ? 'Save' : 'Saved'}</Button>
            <Button variant="ghost" onClick={runNow}
                    disabled={running || !st?.rclone?.available || !st?.configured}>
              {running ? 'Backing up…' : 'Back up now'}</Button>
          </div>
        </form>
      )}
      {runMsg && <p className={runMsg.error ? 'error' : 'dim small'}>{runMsg.text}</p>}

      <p className="dim small">
        Restore is command-line only: <code>python -m backend.cli restore</code> on
        the server.
      </p>
    </Card>
  )
}

function fromCfg(c) {
  return { remote: c.remote || '', include_secrets: !!c.include_secrets,
           crypt_remote: c.crypt_remote || '' }
}

function StatusRow({ st, running }) {
  const last = st.last
  return (
    <>
      <div className="row">
        <Tag tone="done">rclone</Tag>
        {st.configured ? <Tag tone="done">remote set</Tag>
          : <Tag tone="pending">no remote</Tag>}
        {st.include_secrets && <Tag>secrets encrypted</Tag>}
        {running && <Tag tone="running">running</Tag>}
      </div>
      <p className="small">
        {last ? (
          <>
            Last run {ts(last.finished_at || last.started_at)} UTC{' '}
            <Tag tone={last.ok ? 'done' : 'error'}>{last.ok ? 'ok' : 'error'}</Tag>
            {last.ok && <span className="dim"> · {human(last.bytes)}</span>}
          </>
        ) : <span className="dim">Never run.</span>}
        <span className="dim">
          {' · '}{st.next_scheduled ? `next ${ts(st.next_scheduled)} UTC`
            : 'no timer scheduled'}</span>
      </p>
      {last && !last.ok && last.error && <p className="error small">{last.error}</p>}
    </>
  )
}
