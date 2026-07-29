import { useEffect, useState } from 'react'
import { api } from '../api.js'

// The operator's control surface for computer use. Two things live here that
// exist nowhere else: the folder grants (no tool can create one — if the agent
// could widen its own reach the grant would be decoration) and the pairing
// token a desktop client needs to connect.
export default function ComputerUse() {
  const [state, setState] = useState(null)
  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [probe, setProbe] = useState(null)
  const [probing, setProbing] = useState(false)
  const [root, setRoot] = useState('')
  const [label, setLabel] = useState('')
  const [jf, setJf] = useState({ url: '', key_set: false })
  const [jfKey, setJfKey] = useState('')
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState('')

  const refresh = () => api('/api/computeruse/status').then(setState)
  useEffect(() => {
    refresh()
    api('/api/computeruse/token').then((r) => setToken(r.token)).catch(() => {})
    api('/api/computeruse/jellyfin').then(setJf).catch(() => {})
    const t = setInterval(refresh, 8000)
    return () => clearInterval(t)
  }, [])

  const note = (m) => { setSaved(m); setTimeout(() => setSaved(''), 3000) }

  async function addGrant(e) {
    e.preventDefault()
    setError(null)
    try {
      await api('/api/computeruse/grants', {
        method: 'POST', body: JSON.stringify({ root, label }) })
      setRoot(''); setLabel('')
      refresh()
    } catch (err) { setError(err.detail || String(err)) }
  }

  async function revoke(id, r) {
    if (!window.confirm(`Stop Jarvis reaching ${r}?`)) return
    await api(`/api/computeruse/grants/${id}`, { method: 'DELETE' })
    refresh()
  }

  async function runProbe(clientId) {
    setProbing(true); setError(null); setProbe(null)
    try {
      const q = clientId ? `?client_id=${encodeURIComponent(clientId)}` : ''
      const r = await api(`/api/computeruse/probe${q}`, { method: 'POST' })
      setProbe(r.result || r)
    } catch (err) { setError(err.detail || String(err)) }
    setProbing(false)
  }

  async function saveJellyfin(e) {
    e.preventDefault()
    setError(null)
    try {
      setJf(await api('/api/computeruse/jellyfin', {
        method: 'PUT', body: JSON.stringify({ url: jf.url, key: jfKey }) }))
      setJfKey('')
      note('Jellyfin settings saved')
    } catch (err) { setError(err.detail || String(err)) }
  }

  async function rotate() {
    if (!window.confirm('Rotate the pairing token? Every connected client is '
      + 'dropped and has to be restarted with the new one.')) return
    const r = await api('/api/computeruse/token', {
      method: 'POST', body: JSON.stringify({ rotate: true }) })
    setToken(r.token)
    setShowToken(true)
    note('token rotated')
  }

  if (!state) return <div className="page"><p className="dim">loading…</p></div>

  const clients = state.clients || []
  const cmd = `python3 clients/computeruse/agent.py \\\n`
    + `  --server ${window.location.origin} \\\n`
    + `  --token ${showToken ? token : '<token>'} \\\n`
    + (state.grants || []).map((g) => `  --allow-root ${g.root}`).join(' \\\n')

  return (
    <div className="page cu-page">
      <h1>Computer use</h1>
      <p className="dim">
        Jarvis drives a computer you are running the client on — the system
        volume, whatever is playing, and media from the folders you grant below.
        There is no shell: the client accepts a fixed list of actions and
        nothing else.
      </p>
      {error && <p className="error">{error}</p>}
      {saved && <p className="badge">{saved}</p>}

      <section className="panel">
        <h2>Connected computers</h2>
        {clients.length === 0 ? (
          <p className="dim">
            Nothing connected. Run the client on the machine you want driven —
            it dials out to Jarvis, so no port has to be open on it.
          </p>
        ) : (
          <ul className="cu-clients">
            {clients.map((c) => (
              <li key={c.id}>
                <span className="run-dot running" />
                <span className="grow">
                  <strong>{c.name}</strong> <span className="dim">{c.platform}</span>
                  {c.caps?.dry_run && <span className="tag">dry run</span>}
                </span>
                <button className="ghost" disabled={probing}
                        onClick={() => runProbe(c.id)}>
                  {probing ? 'asking…' : 'what can it drive?'}</button>
              </li>
            ))}
          </ul>
        )}
        {probe && (
          <div className="cu-probe">
            <div><span className="dim">screens</span>{' '}
              {(probe.screens || []).length
                ? probe.screens.map((s) => `${s.index}: ${s.id} ${s.geometry || ''}`).join(' · ')
                : <span className="dim">none detected</span>}</div>
            <div><span className="dim">audio out</span>{' '}
              {(probe.audio_devices || []).length
                ? probe.audio_devices.map((a) => a.label || a.id).join(' · ')
                : <span className="dim">none detected</span>}</div>
            <div><span className="dim">players</span>{' '}
              {(probe.players || []).join(' · ') || <span className="dim">none running</span>}</div>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Folders Jarvis may play from</h2>
        <p className="dim small">
          The only places on your disk it can reach. Nothing else is visible to
          it, and no tool can add one — this form is the only way in. The client
          also enforces its own <code>--allow-root</code> ceiling, so a grant
          for a folder it was not started with is ignored.
        </p>
        {(state.grants || []).length === 0
          ? <p className="dim">No folders granted — media playback is off.</p>
          : (
            <ul className="cu-grants">
              {state.grants.map((g) => (
                <li key={g.id}>
                  <code className="grow">{g.root}</code>
                  {g.label && <span className="tag">{g.label}</span>}
                  <button className="ghost danger"
                          onClick={() => revoke(g.id, g.root)}>revoke</button>
                </li>
              ))}
            </ul>
          )}
        <form className="row" onSubmit={addGrant}>
          <input className="grow" placeholder="/home/you/Music" value={root}
                 onChange={(e) => setRoot(e.target.value)} />
          <input placeholder="label (optional)" value={label}
                 onChange={(e) => setLabel(e.target.value)} />
          <button type="submit" disabled={!root.trim()}>Grant</button>
        </form>
      </section>

      <section className="panel">
        <h2>Jellyfin</h2>
        <p className="dim small">
          A second place to play from. The API key stays on the Jarvis host —
          the client is handed a stream URL, never the key.
        </p>
        <form className="row" onSubmit={saveJellyfin}>
          <input className="grow" placeholder="https://jellyfin.example"
                 value={jf.url}
                 onChange={(e) => setJf({ ...jf, url: e.target.value })} />
          <input type="password"
                 placeholder={jf.key_set ? 'API key (stored)' : 'API key'}
                 value={jfKey} onChange={(e) => setJfKey(e.target.value)} />
          <button type="submit">Save</button>
        </form>
      </section>

      <section className="panel">
        <h2>Connect a computer</h2>
        <div className="row">
          <button className="ghost" onClick={() => setShowToken((s) => !s)}>
            {showToken ? 'hide token' : 'reveal token'}</button>
          <button className="ghost danger" onClick={rotate}>rotate token</button>
        </div>
        <pre className="cu-cmd">{cmd}</pre>
        <p className="dim small">
          Add <code>--dry-run</code> to have it report what it would do without
          touching the machine. Closing the client ends all access immediately.
        </p>
      </section>
    </div>
  )
}
