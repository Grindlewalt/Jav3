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
  const [platform, setPlatform] = useState(
    () => (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
      ? 'mac' : 'linux'))
  const [probe, setProbe] = useState(null)
  const [probing, setProbing] = useState(false)
  const [root, setRoot] = useState('')
  const [label, setLabel] = useState('')
  const [jf, setJf] = useState({ url: '', key_set: false })
  const [jfKey, setJfKey] = useState('')
  // Cloudflare Access service token, if Jarvis sits behind Access. Held in this
  // component and nowhere else: not sent to the backend, not in localStorage, and
  // gone when the page reloads. It exists only to paste into the command below —
  // that is why the field is here rather than a stored setting.
  const [tm, setTm] = useState({ url: '', cf_id: '', secret_set: false })
  const [tmSecret, setTmSecret] = useState('')
  const [tmTest, setTmTest] = useState(null)
  const [cfId, setCfId] = useState('')
  const [cfSecret, setCfSecret] = useState('')
  const [machine, setMachine] = useState('')
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState('')

  const refresh = () => api('/api/computeruse/status').then(setState)
  useEffect(() => {
    refresh()
    api('/api/computeruse/token').then((r) => setToken(r.token)).catch(() => {})
    api('/api/computeruse/jellyfin').then(setJf).catch(() => {})
    api('/api/computeruse/tarmac').then(setTm).catch(() => {})
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

  async function saveTarmac(e) {
    e.preventDefault()
    setError(null); setTmTest(null)
    try {
      setTm(await api('/api/computeruse/tarmac', {
        method: 'PUT',
        body: JSON.stringify({ url: tm.url, cf_id: tm.cf_id, cf_secret: tmSecret }) }))
      setTmSecret('')
      note('music server saved')
    } catch (err) { setError(err.detail || String(err)) }
  }

  async function testTarmac() {
    setTmTest({ testing: true })
    try {
      setTmTest(await api('/api/computeruse/tarmac/test', { method: 'POST' }))
    } catch (err) { setTmTest({ ok: false, error: err.detail || String(err) }) }
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
  const tok = showToken ? token : '<reveal the token above>'
  const roots = (state.grants || []).map((g) => g.root)
  const origin = window.location.origin
  const behindAccess = !!(cfId && cfSecret)
  // Access headers, if the operator pasted a service token. Only ever used to
  // build these strings — nothing here is sent to Jarvis.
  const curlAuth = behindAccess
    ? ` \\\n  -H 'CF-Access-Client-Id: ${cfId}' \\\n  -H 'CF-Access-Client-Secret: ${cfSecret}'`
    : ''
  const fetchCmd =
    `mkdir -p ~/jarvis-client && cd ~/jarvis-client\n`
    + `curl -fsSL '${origin}/api/computeruse/client.zip'${curlAuth} -o c.zip \\\n`
    + `  && unzip -o c.zip && rm c.zip\n`
    + `python3 -m pip install -r computeruse/requirements.txt`
  const doctorCmd = 'python3 ~/jarvis-client/computeruse/agent.py --selftest'
  // the grants become --allow-root, since the client treats those flags as its
  // ceiling — a grant can narrow it, never widen it
  const installCmd = [
    'python3 ~/jarvis-client/computeruse/agent.py --install',
    `  --server ${origin}`,
    `  --token ${tok}`,
    ...(machine ? [`  --name ${machine}`] : []),
    ...(roots.length ? roots.map((r) => `  --allow-root ${r}`)
                     : ['  --allow-root ~/Music   # grant a folder below first']),
    ...(behindAccess ? [`  --cf-access-id ${cfId}`,
                        `  --cf-access-secret ${cfSecret}`] : []),
  ].join(' \\\n')

  const enable = platform === 'mac' ? [
    'launchctl bootstrap gui/$UID \\',
    '  ~/Library/LaunchAgents/network.atomos.jarvis.computeruse.plist',
    'launchctl kickstart -p gui/$UID/network.atomos.jarvis.computeruse',
    '',
    '# stop it:  launchctl bootout gui/$UID/network.atomos.jarvis.computeruse',
    '# logs:     tail -f ~/Library/Logs/jarvis-computeruse.log',
  ].join('\n') : [
    'systemctl --user daemon-reload',
    'systemctl --user enable --now jarvis-computeruse.service',
    'loginctl enable-linger $USER      # keep running after logout',
    '',
    '# logs:  journalctl --user -u jarvis-computeruse.service -f',
  ].join('\n')

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
            {/* two lists, not one: the mixer sinks and mpv's outputs are
                separate namespaces and are not interchangeable */}
            <div><span className="dim">mixer (volume)</span>{' '}
              {(probe.audio_devices || []).length
                ? probe.audio_devices.map((a) => a.label || a.id).join(' · ')
                : <span className="dim">none detected</span>}</div>
            <div><span className="dim">outputs (playback)</span>{' '}
              {(probe.play_devices || []).length
                ? probe.play_devices.slice(0, 8).map((a) => a.id).join(' · ')
                : <span className="dim">none detected — is mpv installed?</span>}</div>
            <div><span className="dim">players</span>{' '}
              {(probe.players || []).join(' · ') || <span className="dim">none running</span>}</div>
            <div><span className="dim">reachable folders</span>{' '}
              {(probe.roots || []).join(' · ')
                || <span className="dim">none — nothing on disk is playable</span>}</div>
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
        <h2>Music server (TARMAC)</h2>
        <p className="dim small">
          The operator's own library, at <code>music.atomos.network</code>.
          Jarvis talks to it over HTTP and plays on <em>its</em> players — the
          music app open on a phone or desktop — so nothing streams through a
          computer-use client and no credential reaches a media player.
          <br />
          Its Cloudflare Access application is <strong>separate from this
          one</strong>, so it needs its own Service Auth policy naming the
          service token, even if the same token works for Jarvis.
        </p>
        <form className="row" onSubmit={saveTarmac}>
          <input className="grow" placeholder="https://music.atomos.network"
                 value={tm.url}
                 onChange={(e) => setTm({ ...tm, url: e.target.value })} />
          <input placeholder="CF-Access-Client-Id" value={tm.cf_id}
                 onChange={(e) => setTm({ ...tm, cf_id: e.target.value })} />
          <input type="password"
                 placeholder={tm.secret_set ? 'secret (stored)' : 'CF-Access-Client-Secret'}
                 value={tmSecret} onChange={(e) => setTmSecret(e.target.value)} />
          <button type="submit">Save</button>
          <button type="button" className="ghost" onClick={testTarmac}
                  disabled={!tm.url}>Test</button>
        </form>
        {tmTest && (
          <p className={tmTest.ok ? 'badge' : 'error'}>
            {tmTest.testing ? 'asking…'
              : tmTest.ok
                ? `reachable — ${tmTest.status?.tracks ?? '?'} tracks, `
                  + `${tmTest.status?.players_connected ?? 0} player(s) open`
                : tmTest.error}
          </p>
        )}
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
          <span className="grow" />
          <div className="cu-plat">
            {[['linux', 'Linux'], ['mac', 'macOS']].map(([k, label]) => (
              <button key={k} className={platform === k ? 'on' : ''}
                      onClick={() => setPlatform(k)}>{label}</button>
            ))}
          </div>
        </div>

        <div className="cu-setup-fields">
          <label>
            <span>Name this machine</span>
            <input placeholder="macbook" value={machine}
                   onChange={(e) => setMachine(e.target.value.replace(/[^\w.-]/g, ''))} />
            <span className="dim small">What Jarvis will call it. Connect several
              and it picks by name.</span>
          </label>
          <label>
            <span>Cloudflare Access service token <span className="dim">(only if
              Jarvis is behind Access)</span></span>
            <div className="row">
              <input className="grow" placeholder="<id>.access" value={cfId}
                     onChange={(e) => setCfId(e.target.value.trim())} />
              <input className="grow" type="password" placeholder="client secret"
                     value={cfSecret}
                     onChange={(e) => setCfSecret(e.target.value.trim())} />
            </div>
            <span className="dim small">
              Held in this page only — not sent to Jarvis, not saved, gone when
              you reload. It exists to build the commands below.
            </span>
          </label>
        </div>

        <ol className="cu-steps">
          <li>
            <strong>Get the client onto that machine.</strong>
            <pre className="cu-cmd">{fetchCmd}</pre>
            <span className="dim small">
              Downloads the client from this Jarvis and installs its
              dependencies. {behindAccess
                ? 'The Access headers are included above.'
                : 'If Jarvis is behind Cloudflare Access, fill the token in above and this command will carry it.'}
            </span>
          </li>
          <li>
            <strong>Ask it what is missing.</strong>
            <pre className="cu-cmd">{doctorCmd}</pre>
            <span className="dim small">
              Prints what this machine can already drive, then a
              copy-and-paste list of the exact install commands for whatever it
              cannot. Re-run it until that list is empty.
            </span>
          </li>
          <li>
            <strong>Save the settings and write the service.</strong>
            <pre className="cu-cmd">{installCmd}</pre>
            <span className="dim small">
              {roots.length === 0 && <><strong>Grant a folder above first</strong> —
                without one, nothing on that machine is playable. </>}
              Settings go to <code>~/.config/jarvis/computeruse.json</code> at{' '}
              <strong>0600</strong>; the service file gets a path to it and
              nothing else, because units and plists are world-readable. After
              this the client runs with no arguments.
            </span>
          </li>
          <li>
            <strong>Start it, and keep it started.</strong>
            <pre className="cu-cmd">{enable}</pre>
            <span className="dim small">
              {platform === 'mac'
                ? <>Media keys need Accessibility permission, granted to the{' '}
                    <em>python binary</em> — change interpreter and you grant it
                    again. On macOS 15 it also lapses after a reboot.</>
                : <>Tied to <code>graphical-session.target</code>, since opening a
                    link or playing video needs a display. If those fail but
                    volume works:{' '}
                    <code>systemctl --user import-environment DISPLAY
                    WAYLAND_DISPLAY XAUTHORITY</code></>}
            </span>
          </li>
        </ol>

        <p className="dim small">
          Add <code>--dry-run</code> anywhere to have it report what it would do
          without touching the machine. Quitting the client — or stopping the
          service — ends all access immediately.
        </p>
      </section>
    </div>
  )
}
