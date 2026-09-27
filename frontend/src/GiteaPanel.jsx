/* Settings > Gitea: is the host git server up, which repos it holds, and the
 * account basics (create, reset a password, disable). Everything goes through
 * /api/gitea, operator-only; the admin token never reaches the browser. */
import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { useAsk } from './ask.jsx'
import { notifyError } from './notify.js'
import { Button, Card, EmptyState, Input, Tag } from './components/index.js'

export default function GiteaPanel() {
  const ask = useAsk()
  const [st, setSt] = useState(null)
  const [repos, setRepos] = useState(null)
  const [users, setUsers] = useState(null)
  const [form, setForm] = useState({ login: '', email: '', password: '' })
  const [reset, setReset] = useState({ login: '', password: '' })

  const load = useCallback(() => {
    api('/api/gitea/status').then((s) => {
      setSt(s)
      if (!s.configured) return
      api('/api/gitea/repos').then((r) => setRepos(r.repos || [])).catch(() => setRepos([]))
      api('/api/gitea/users').then((r) => setUsers(r.users || [])).catch(() => setUsers([]))
    }).catch(() => setSt({ configured: false }))
  }, [])
  useEffect(() => { load() }, [load])

  async function create(e) {
    e.preventDefault()
    try {
      await api('/api/gitea/users', { method: 'POST', body: JSON.stringify(form) })
      setForm({ login: '', email: '', password: '' })
      load()
    } catch (err) { notifyError(err) }
  }
  async function savePassword(e) {
    e.preventDefault()
    try {
      await api(`/api/gitea/users/${encodeURIComponent(reset.login)}/password`, {
        method: 'POST', body: JSON.stringify({ password: reset.password }) })
      setReset({ login: '', password: '' })
    } catch (err) { notifyError(err) }
  }
  async function toggle(u) {
    const disabled = !u.prohibit_login
    if (disabled && !await ask.confirm(`Disable ${u.login}? They can no longer sign in.`,
                                       { confirmLabel: 'Disable', danger: true })) return
    try {
      await api(`/api/gitea/users/${encodeURIComponent(u.login)}/disable`, {
        method: 'POST', body: JSON.stringify({ disabled }) })
      load()
    } catch (err) { notifyError(err) }
  }

  if (!st) return null
  return (
    <Card title="Gitea" headingLevel={2}>
      {!st.configured ? (
        <p className="dim small settings-note">
          Not set up. Agents can still ask for commits. To give them pull requests
          for you to review, run <code>python -m backend.cli gitea-setup</code> on
          the host, then restart Jav3.
        </p>
      ) : (
        <>
          <p className="small settings-note">
            {st.running ? <Tag>running</Tag> : <Tag>not answering</Tag>}
            {' '}{st.version ? `v${st.version} · ` : ''}
            <a href={st.url} target="_blank" rel="noreferrer">{st.url}</a>
            {' '}· owner <strong>{st.owner}</strong> · agents push as <code>{st.bot}</code>
            {' '}to <code>agent/*</code> branches only
          </p>
          {st.tokens_private === false && (
            <p className="warn settings-note">The Gitea token files are readable by
              other users on the host: chmod 600 them.</p>)}
          <h3 className="small">Repos</h3>
          {repos === null ? <EmptyState>loading…</EmptyState>
            : repos.length === 0 ? <EmptyState>No repos yet. One is made per project
              the first time it is pushed.</EmptyState>
            : (
              <ul className="device-list">
                {repos.map((r) => (
                  <li key={r.name} className="device-row">
                    <a className="ellipsis" href={r.url} target="_blank" rel="noreferrer">{r.name}</a>
                    {r.private && <Tag>private</Tag>}
                  </li>
                ))}
              </ul>
            )}
          <h3 className="small">Accounts</h3>
          {users === null ? <EmptyState>loading…</EmptyState> : (
            <ul className="device-list">
              {users.map((u) => (
                <li key={u.login} className="device-row">
                  <div className="device-main">
                    <div className="device-name">
                      <strong className="ellipsis">{u.login}</strong>
                      {u.is_admin && <Tag>admin</Tag>}
                      {u.bot && <Tag>agent bot</Tag>}
                      {u.prohibit_login && <Tag>disabled</Tag>}
                    </div>
                  </div>
                  {!u.bot && u.login !== st.owner && (
                    <>
                      <Button variant="ghost"
                              onClick={() => setReset({ login: u.login, password: '' })}>
                        Reset password</Button>
                      <Button variant="ghost" danger={!u.prohibit_login}
                              onClick={() => toggle(u)}>
                        {u.prohibit_login ? 'Enable' : 'Disable'}</Button>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}
          {reset.login && (
            <form className="settings-inline" onSubmit={savePassword}>
              <Input type="password" aria-label={`New password for ${reset.login}`}
                     placeholder={`new password for ${reset.login}`} autoComplete="new-password"
                     value={reset.password}
                     onChange={(e) => setReset({ ...reset, password: e.target.value })} />
              <div className="settings-inline-actions">
                <Button type="submit" disabled={reset.password.length < 8}>Set</Button>
                <Button variant="ghost" onClick={() => setReset({ login: '', password: '' })}>
                  Cancel</Button>
              </div>
            </form>
          )}
          <form className="settings-inline" onSubmit={create}>
            <Input aria-label="New Gitea username" placeholder="username" value={form.login}
                   onChange={(e) => setForm({ ...form, login: e.target.value })} />
            <Input aria-label="Email" placeholder="email (optional)" value={form.email}
                   onChange={(e) => setForm({ ...form, email: e.target.value })} />
            <Input type="password" aria-label="Password" placeholder="password (8+)"
                   autoComplete="new-password" value={form.password}
                   onChange={(e) => setForm({ ...form, password: e.target.value })} />
            <div className="settings-inline-actions">
              <Button type="submit" disabled={!form.login || form.password.length < 8}>
                Add user</Button>
            </div>
          </form>
        </>
      )}
    </Card>
  )
}
