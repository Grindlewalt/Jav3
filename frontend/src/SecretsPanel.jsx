import { useEffect, useState } from 'react'
import { api } from './api.js'
import { notifyError } from './notify.js'
import { useAsk } from './ask.jsx'
import { Button, Card, EmptyState, Input } from './components/index.js'

// The operator's API keys (/api/secrets). The agent can reference one as
// {{secret:NAME}} and the host swaps the value in on the wire — web_read only
// on the hosts a key is bound to, the egress proxy only for projects granted
// it (that grant lives on each project's Network policy, not here). The value
// never comes back out: the API returns a name, the last four characters and
// the bound hosts, and that is all this panel ever holds.
//
// It used to be a strip in the Memory page's sidebar driven by three chained
// prompt dialogs; it is a Review tab now, because what a key may reach is a
// security decision, and it sits beside the network policy it feeds.

const splitHosts = (raw) => raw.split(',').map((h) => h.trim()).filter(Boolean)
const EMPTY = { name: '', value: '', hosts: '' }

export default function SecretsPanel() {
  const [secrets, setSecrets] = useState(null)   // null = still loading
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState(EMPTY)
  const [editing, setEditing] = useState(null)   // name whose hosts are open
  const [hostsDraft, setHostsDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const ask = useAsk()

  const refresh = () => api('/api/secrets')
    .then((r) => setSecrets(r.secrets || []))
    .catch((e) => { setSecrets([]); notifyError(e) })
  useEffect(() => { refresh() }, [])

  async function put(name, body) {
    setBusy(true)
    try {
      await api(`/api/secrets/${encodeURIComponent(name)}`, {
        method: 'PUT', body: JSON.stringify(body) })
      await refresh()
      return true
    } catch (e) { notifyError(e); return false }
    finally { setBusy(false) }
  }

  async function add(e) {
    e.preventDefault()
    const name = form.name.trim()
    if (!name || !form.value) return
    if (await put(name, { value: form.value, hosts: splitHosts(form.hosts) })) {
      setForm(EMPTY); setAdding(false)
    }
  }

  function openHosts(s) {
    setEditing(s.name); setHostsDraft((s.hosts || []).join(', '))
  }
  async function saveHosts(e) {
    e.preventDefault()
    // value '' = keep the stored value; this is a hosts-only edit
    if (await put(editing, { value: '', hosts: splitHosts(hostsDraft) })) setEditing(null)
  }

  async function del(name) {
    if (!await ask.confirm(`Delete secret ${name}?`,
                           { confirmLabel: 'Delete', danger: true })) return
    try {
      await api(`/api/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
      await refresh()
    } catch (e) { notifyError(e) }
  }

  return (
    // No title: the Review tab strip already says "Secrets" (a tab paints no
    // heading of its own). The lede and the add button share the head row.
    <Card className="secrets-card">
      <div className="toolbar secrets-head">
        <p className="dim small secrets-lede">
          Keys the agent uses as {'{{secret:NAME}}'} without ever reading them.
          A key with no web hosts bound is unusable — web_read refuses it.
        </p>
        {!adding && (
          <Button variant="ghost" onClick={() => setAdding(true)}>+ add secret</Button>)}
      </div>

      {adding && (
        <form className="secrets-form" onSubmit={add}>
          <Input label="Name" placeholder="e.g. TBA_KEY" autoFocus required
                 value={form.name}
                 onChange={(e) => setForm({ ...form, name: e.target.value })}
                 hint="letters, digits and underscores; stored upper-case" />
          <Input label="Value" type="password" autoComplete="off" required
                 value={form.value}
                 onChange={(e) => setForm({ ...form, value: e.target.value })}
                 hint="stored host-side; the agent only ever sees the name" />
          <Input label="Web hosts" placeholder="e.g. newsapi.org"
                 value={form.hosts}
                 onChange={(e) => setForm({ ...form, hosts: e.target.value })}
                 hint="comma-separated; leave empty to keep it unusable" />
          <div className="secrets-form-actions">
            <Button type="submit" disabled={busy || !form.name.trim() || !form.value}>
              Save secret</Button>
            <Button variant="ghost" disabled={busy}
                    onClick={() => { setAdding(false); setForm(EMPTY) }}>Cancel</Button>
          </div>
        </form>
      )}

      {secrets === null ? null : secrets.length === 0 ? (
        !adding && <EmptyState pad hint="add one above to reference it from a prompt or project">
          no secrets saved</EmptyState>
      ) : (
        <ul className="secrets-list">
          {secrets.map((s) => (
            <li key={s.name} className="sbx-row secrets-row">
              <div className="grow secrets-main">
                <div className="secrets-name">
                  <span className="mono ellipsis">{s.name}</span>
                  <span className="dim small mono">…{s.last4}</span>
                </div>
                {editing === s.name ? (
                  <form className="secrets-hosts-edit" onSubmit={saveHosts}>
                    <Input aria-label={`web hosts for ${s.name}`} autoFocus
                           value={hostsDraft} placeholder="comma-separated; empty = unusable"
                           onChange={(e) => setHostsDraft(e.target.value)} />
                    <Button type="submit" disabled={busy}>Save</Button>
                    <Button variant="ghost" disabled={busy}
                            onClick={() => setEditing(null)}>Cancel</Button>
                  </form>
                ) : (
                  <div className="small dim ellipsis"
                       title={s.hosts?.length ? s.hosts.join(', ') : undefined}>
                    {s.hosts?.length
                      ? <>web: <span className="mono">{s.hosts.join(', ')}</span></>
                      : 'no web hosts bound — unusable'}
                  </div>
                )}
              </div>
              {editing !== s.name && (
                <div className="sbx-actions">
                  <Button variant="ghost" onClick={() => openHosts(s)}>hosts</Button>
                  <Button variant="ghost" danger onClick={() => del(s.name)}>delete</Button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
