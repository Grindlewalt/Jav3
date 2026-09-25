import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'
import EmptyState from '../components/EmptyState.jsx'

// A Workspace board panel, split out of pages/Workspace.jsx so the shell's
// dock can mount it too. Same component, same props, both surfaces.

// Per-project secret grants: which of the operator's saved keys the egress
// proxy may inject into THIS project's outbound requests ({{secret:X}} swapped
// on the wire — the agent never holds the value). Keys themselves are added in
// Security → Secrets; this panel only flips the grant.
export default function GrantsPanel({ slug }) {
  const [secrets, setSecrets] = useState([])
  const [grants, setGrants] = useState({})   // name -> status
  const [busy, setBusy] = useState(false)

  const refresh = () => Promise.all([
    api('/api/secrets').then((r) => setSecrets(r.secrets)).catch(() => {}),
    api(`/api/egress/grants/${slug}`)
      .then((r) => setGrants(Object.fromEntries(r.grants.map((g) => [g.secret_name, g.status]))))
      .catch(() => {}),
  ])
  useEffect(() => { refresh() }, [slug]) // eslint-disable-line

  async function setGrant(name, status) {
    setBusy(true)
    try {
      await api(`/api/egress/grants/${slug}`, {
        method: 'POST', body: JSON.stringify({ secret: name, status }) })
      await refresh()
    } catch (err) { notifyError(err) }
    setBusy(false)
  }

  return (
    <div className="pane-col">
      <div className="row">
        <span className="grow dim">keys this project may use</span>
        <button className="ghost" onClick={refresh}>↻</button>
      </div>
      <div className="dim small">a granted key is injected wherever this project's code
        sends {'{{secret:NAME}}'} through the egress proxy — the agent never sees the
        value. Add or edit the keys themselves in Security → Secrets.</div>
      <ul className="staged-list">
        {secrets.length === 0 && <EmptyState as="li">no keys saved yet — add them in Security → Secrets</EmptyState>}
        {secrets.map((s) => {
          const granted = grants[s.name] === 'granted'
          return (
            <li key={s.name} className="grant-row">
              <span className={`tag ${granted ? 'done' : ''}`}>{granted ? 'granted' : 'off'}</span>
              <span className="grow grant-main">
                <span className="grant-name">
                  <span className="mono ellipsis" title={s.name}>{s.name}</span>
                  <span className="dim small mono">…{s.last4}</span>
                </span>
                {s.hosts?.length > 0 &&
                  <span className="dim small ellipsis" title={`web: ${s.hosts.join(', ')}`}>
                    {s.hosts.join(', ')}</span>}
              </span>
              <button className={granted ? 'win-btn' : 'win-btn ok'} disabled={busy}
                      onClick={() => setGrant(s.name, granted ? 'revoked' : 'granted')}>
                {granted ? 'revoke' : 'grant'}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
