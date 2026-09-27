import { useEffect, useState } from 'react'
import { api } from './api.js'
import { notifyError } from './notify.js'
import { Button, Card, EmptyState } from './components/index.js'
import { ts } from './format.js'

// Settings: the "Yes, always allow this and similar" rules a permission ask
// stored (backend/permissions.py) — exact tool + normalised prefix, per
// project. Each is revocable; revoking means the next such call asks again.
export default function PermissionRulesPanel() {
  const [rules, setRules] = useState(null)
  const load = () => api('/api/permissions/rules').then((r) => setRules(r.rules))
    .catch(() => setRules([]))
  useEffect(() => { load() }, [])

  async function revoke(id) {
    try {
      await api(`/api/permissions/rules/${id}`, { method: 'DELETE' })
    } catch (err) { notifyError(err) }
    load()
  }

  return (
    <Card title="Permission rules" headingLevel={2}>
      <p className="dim">
        Calls you chose to always allow from a permission ask (chats in auto or ask
        mode, set in the chat toolbar or with Shift+Tab in the terminal). Each rule is
        one tool and one command or path prefix, in one project.
      </p>
      {rules && rules.length === 0 && <EmptyState>no always-allow rules</EmptyState>}
      {rules && rules.length > 0 && (
        <ul className="perm-rules">
          {rules.map((r) => (
            <li key={r.id} className="row">
              <code>{r.tool}</code>
              <span className="grow ellipsis"><code>{r.prefix}</code></span>
              <span className="dim">{r.project_slug || 'no project'}</span>
              <span className="dim">{ts(r.created_at)}</span>
              <Button variant="ghost" onClick={() => revoke(r.id)}>Revoke</Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
