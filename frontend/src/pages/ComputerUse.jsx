import { useEffect, useState } from 'react'
import { api } from '../api.js'

// Cloudflare's dashboard shows a service token as whole header lines, so that is
// what gets pasted. Strip the header name, quotes and whitespace rather than
// letting "CF-Access-Client-Id: abc.access" through as the id.
function cleanToken(v) {
  return String(v || '')
    .replace(/^\s*CF[-_]?Access[-_]?Client[-_]?(Id|Secret)\s*[:=]\s*/i, '')
    .replace(/^["'`]|["'`]$/g, '')
    .replace(/\s+/g, '')
    .trim()
}

// Copies what it is GIVEN, not what is on screen. The set-up command renders a
// placeholder until the token is revealed, and copying the rendered text meant
// pasting the literal "<reveal the token above>" into a terminal.
function Copy({ text, label = 'copy' }) {
  const [done, setDone] = useState(false)
  return (
    <button type="button" className="copy-btn" onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
      } catch {
        const ta = document.createElement('textarea')   // no secure context
        ta.value = text
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        ta.remove()
      }
      setDone(true)
      setTimeout(() => setDone(false), 1600)
    }}>{done ? 'copied' : label}</button>
  )
}

const Block = ({ text }) => (
  <div className="cu-block"><Copy text={text} /><pre>{text}</pre></div>
)

export default function ComputerUse() {
  const [state, setState] = useState(null)
  const [token, setToken] = useState('')
  const [open, setOpen] = useState(null)      // expanded machine
  const [probe, setProbe] = useState({})
  const [setupOpen, setSetupOpen] = useState(false)
  const [msg, setMsg] = useState(null)
  const [tm, setTm] = useState({ url: '', cf_id: '', secret_set: false })
  const [tmSecret, setTmSecret] = useState('')
  const [tmTest, setTmTest] = useState(null)
  const [jf, setJf] = useState({ url: '', key_set: false })
  const [jfKey, setJfKey] = useState('')

  const refresh = () => api('/api/computeruse/status').then(setState)
  useEffect(() => {
    refresh()
    api('/api/computeruse/token').then((r) => setToken(r.token)).catch(() => {})
    api('/api/computeruse/tarmac').then(setTm).catch(() => {})
    api('/api/computeruse/jellyfin').then(setJf).catch(() => {})
    const t = setInterval(refresh, 6000)
    return () => clearInterval(t)
  }, [])

  const say = (m) => { setMsg(m); setTimeout(() => setMsg(null), 6000) }

  async function runProbe(name) {
    setProbe((p) => ({ ...p, [name]: { loading: true } }))
    try {
      const r = await api(
        `/api/computeruse/probe?client_id=${encodeURIComponent(name)}`,
        { method: 'POST' })
      setProbe((p) => ({ ...p, [name]: r.result || r }))
    } catch (err) {
      setProbe((p) => ({ ...p, [name]: { error: err.detail || String(err) } }))
    }
  }

  async function togglePriv(client, capability, allowed) {
    try {
      await api('/api/computeruse/privileges', {
        method: 'PUT', body: JSON.stringify({ client, capability, allowed }) })
      refresh()
    } catch (err) { say(err.detail || String(err)) }
  }

  async function addFolder(client, root) {
    try {
      const r = await api('/api/computeruse/grants', {
        method: 'POST', body: JSON.stringify({ root, client }) })
      refresh()
      if (r.restart_needed) {
        say(`Added. ${client} picks up folders when it reconnects — restart the `
          + `client to use this one.`)
      }
    } catch (err) { say(err.detail || String(err)) }
  }

  const revoke = async (id) => {
    await api(`/api/computeruse/grants/${id}`, { method: 'DELETE' })
    refresh()
  }

  async function saveMusic(e) {
    e.preventDefault()
    setTmTest(null)
    try {
      setTm(await api('/api/computeruse/tarmac', {
        method: 'PUT',
        body: JSON.stringify({ url: tm.url, cf_id: tm.cf_id,
                               cf_secret: tmSecret }) }))
      setTmSecret('')
      say('Music server saved')
    } catch (err) { say(err.detail || String(err)) }
  }

  async function testMusic() {
    setTmTest({ testing: true })
    try {
      setTmTest(await api('/api/computeruse/tarmac/test', { method: 'POST' }))
    } catch (err) { setTmTest({ ok: false, error: err.detail || String(err) }) }
  }

  async function saveJellyfin(e) {
    e.preventDefault()
    try {
      setJf(await api('/api/computeruse/jellyfin', {
        method: 'PUT', body: JSON.stringify({ url: jf.url, key: jfKey }) }))
      setJfKey('')
      say('Jellyfin saved')
    } catch (err) { say(err.detail || String(err)) }
  }

  if (!state) return <div className="page"><p className="dim">loading…</p></div>
  const machines = state.clients || []
  const caps = state.capabilities || {}
  const orphans = (state.grants || []).filter(
    (g) => g.client && !machines.some((m) => m.name === g.client))

  return (
    <div className="page cu-page">
      <div className="cu-head">
        <h1>Computer use</h1>
        <button onClick={() => setSetupOpen(true)}>Connect a computer</button>
      </div>
      {msg && <p className="warn">{msg}</p>}

      {machines.length === 0 ? (
        <section className="panel cu-empty">
          <p>No computer connected.</p>
          <p className="dim small">
            Jarvis can only reach a machine running the client. It dials out, so
            nothing needs to be open on your side.
          </p>
        </section>
      ) : machines.map((m) => (
        <Machine key={m.id} m={m} caps={caps}
                 expanded={open === m.name}
                 onToggle={() => setOpen(open === m.name ? null : m.name)}
                 probe={probe[m.name]} onProbe={() => runProbe(m.name)}
                 onPriv={togglePriv} onAdd={addFolder} onRevoke={revoke} />
      ))}

      {orphans.length > 0 && (
        <section className="panel">
          <h2>Folders for computers that aren’t connected</h2>
          <ul className="cu-grants">
            {orphans.map((g) => (
              <li key={g.id}>
                <code className="grow">{g.root}</code>
                <span className="tag">{g.client}</span>
                <button className="ghost danger" onClick={() => revoke(g.id)}>
                  remove</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="panel">
        <h2>Music server</h2>
        <form className="row" onSubmit={saveMusic}>
          <input className="grow" placeholder="https://music.atomos.network"
                 value={tm.url}
                 onChange={(e) => setTm({ ...tm, url: e.target.value })} />
          <input placeholder="Client Id" value={tm.cf_id}
                 onChange={(e) => setTm({ ...tm, cf_id: cleanToken(e.target.value) })} />
          <input type="password"
                 placeholder={tm.secret_set ? 'secret (stored)' : 'Client Secret'}
                 value={tmSecret}
                 onChange={(e) => setTmSecret(cleanToken(e.target.value))} />
          <button type="submit">Save</button>
          <button type="button" className="ghost" onClick={testMusic}
                  disabled={!tm.url}>Test</button>
        </form>
        {tmTest && (
          <p className={tmTest.ok ? 'badge' : 'error'}>
            {tmTest.testing ? 'asking…' : tmTest.ok
              ? `${tmTest.status?.tracks ?? '?'} tracks · `
                + `${tmTest.status?.players_connected ?? 0} player(s) open`
              : tmTest.error}
          </p>
        )}
        <p className="dim small">
          Its Cloudflare Access application is separate from this one, so it needs
          its own Service Auth policy even with the same token.
        </p>
      </section>

      <section className="panel">
        <h2>Jellyfin</h2>
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

      {setupOpen && (
        <Setup token={token} machines={machines}
               onClose={() => { setSetupOpen(false); refresh() }} />
      )}
    </div>
  )
}

// --- one computer ------------------------------------------------------------

function Machine({ m, caps, expanded, onToggle, probe, onProbe,
                   onPriv, onAdd, onRevoke }) {
  const [root, setRoot] = useState('')
  const privs = m.privileges || {}
  const off = Object.values(privs).filter((v) => v === false).length
  const folders = m.grants || []

  return (
    <section className="panel cu-machine">
      <button className="cu-machine-head" onClick={onToggle}>
        <span className="run-dot running" />
        <span className="grow">
          <strong>{m.name}</strong>
          <span className="dim"> · {m.platform === 'darwin' ? 'macOS' : m.platform}</span>
          {m.caps?.dry_run && <span className="tag">dry run</span>}
        </span>
        <span className="dim small">
          {folders.length} folder{folders.length === 1 ? '' : 's'}
          {off > 0 && ` · ${off} revoked`}
        </span>
        <span className={expanded ? 'chev open' : 'chev'} aria-hidden="true">›</span>
      </button>

      {expanded && (
        <div className="cu-machine-body">
          <h3>Allowed to</h3>
          <ul className="cu-privs">
            {Object.entries(caps).map(([key, meta]) => {
              const on = privs[key] !== false
              return (
                <li key={key} className={on ? '' : 'revoked'}>
                  <span className="grow">
                    <strong>{meta.label}</strong>
                    <span className="dim small"> {meta.note}</span>
                  </span>
                  <button className={on ? 'ghost danger' : ''}
                          onClick={() => onPriv(m.name, key, !on)}>
                    {on ? 'Revoke' : 'Grant'}</button>
                </li>
              )
            })}
          </ul>

          <h3>Folders on this computer</h3>
          {folders.length === 0
            ? <p className="dim small">None, so nothing on it can be played.</p>
            : (
              <ul className="cu-grants">
                {folders.map((g) => (
                  <li key={g.id}>
                    <code className="grow">{g.root}</code>
                    {!g.client && <span className="tag">all computers</span>}
                    <button className="ghost danger"
                            onClick={() => onRevoke(g.id)}>remove</button>
                  </li>
                ))}
              </ul>
            )}
          <form className="row" onSubmit={(e) => {
            e.preventDefault(); onAdd(m.name, root.trim()); setRoot('')
          }}>
            <input className="grow" value={root}
                   placeholder={m.platform === 'darwin'
                     ? '/Users/you/Movies' : '/home/you/Music'}
                   onChange={(e) => setRoot(e.target.value)} />
            <button type="submit" disabled={!root.trim().startsWith('/')}>
              Add</button>
          </form>

          <h3>Hardware</h3>
          {!probe ? <button className="ghost" onClick={onProbe}>Check</button>
            : probe.loading ? <p className="dim">asking…</p>
            : probe.error ? <p className="error">{probe.error}</p>
            : <Hardware d={probe} />}
        </div>
      )}
    </section>
  )
}

function Hardware({ d }) {
  const screens = d.screens || []
  const mixer = d.audio_devices || []
  const outs = d.play_devices || []
  const row = (label, items, empty) => (
    <>
      <dt>{label}</dt>
      <dd>{items.length ? items : <span className="dim">{empty}</span>}</dd>
    </>
  )
  return (
    <dl className="cu-hw">
      {row('Screens',
        screens.map((s) => (
          <div key={s.index}>Screen {s.index}{s.geometry ? ` — ${s.geometry}` : ''}</div>)),
        'none detected')}
      {/* Two different things, so two plain labels. "Mixer" and "ao device" are
          protocol words that meant nothing to anyone reading this page. */}
      {row('Speakers it can turn up or down',
        mixer.map((a) => <div key={a.id}>{a.label || a.id}</div>),
        'none detected')}
      {row('Speakers it can play through',
        outs.slice(0, 6).map((a) => <div key={a.id}>{a.id}</div>),
        'none — is mpv installed?')}
      {row('Playing now',
        (d.players || []).map((p) => <div key={p}>{p}</div>),
        'nothing')}
    </dl>
  )
}

// --- set-up, one step at a time ----------------------------------------------
// A fixed-height dialog: each step fits, so nothing scrolls and nothing gets
// skipped. The previous version was one long column of prose, which is how a
// placeholder ends up pasted into a terminal instead of a token.

const STEPS = ['Name', 'Access', 'Download', 'Check', 'Keep running']

function Setup({ token, machines, onClose }) {
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [cfId, setCfId] = useState('')
  const [cfSecret, setCfSecret] = useState('')
  const [platform, setPlatform] = useState(
    () => (/Mac/.test(navigator.platform || navigator.userAgent) ? 'mac' : 'linux'))

  const origin = window.location.origin
  const behind = !!(cfId && cfSecret)
  const here = machines.find((m) => m.name === name)
  const cfCurl = behind
    ? ` \\\n  -H 'CF-Access-Client-Id: ${cfId}' \\\n  -H 'CF-Access-Client-Secret: ${cfSecret}'`
    : ''

  // Every step after the download runs the client's OWN interpreter, not
  // whatever `python3` means in that shell:
  //   - `unzip` is not in a base Linux install; tar is. The zip download used
  //     to succeed and then die on `unzip: command not found`.
  //   - `pip install` into the system python is refused outright on Arch and
  //     Debian (PEP 668, "externally-managed-environment"), and into a venv
  //     that happens to be active it installs the deps somewhere unrelated.
  //   - --install bakes sys.executable into the systemd unit / plist, so the
  //     interpreter used here is the one the service will run forever.
  const py = '~/jarvis-client/.venv/bin/python'
  const cmds = {
    fetch: `mkdir -p ~/jarvis-client && cd ~/jarvis-client\n`
      + `curl -fsSL '${origin}/api/computeruse/client.tar.gz' \\\n`
      + `  -H 'X-Jarvis-Token: ${token}'${cfCurl} -o c.tgz \\\n`
      + `  && tar xzf c.tgz && rm -f c.tgz || { rm -f c.tgz; echo FAILED; }\n`
      + `python3 -m venv .venv\n`
      + `.venv/bin/pip install -q -r computeruse/requirements.txt`,
    check: `${py} ~/jarvis-client/computeruse/agent.py --selftest`,
    run: `${py} ~/jarvis-client/computeruse/agent.py`,
    install: [
      `${py} ~/jarvis-client/computeruse/agent.py --install`,
      `  --server ${origin}`,
      `  --token ${token}`,
      ...(name ? [`  --name ${name}`] : []),
      ...(behind ? [`  --cf-access-id ${cfId}`,
                    `  --cf-access-secret ${cfSecret}`] : []),
    ].join(' \\\n'),
    enable: platform === 'mac'
      ? 'launchctl bootstrap gui/$UID '
        + '~/Library/LaunchAgents/network.atomos.jarvis.computeruse.plist\n'
        + 'launchctl kickstart -p gui/$UID/network.atomos.jarvis.computeruse'
      : 'systemctl --user daemon-reload\n'
        + 'systemctl --user enable --now jarvis-computeruse.service\n'
        + 'loginctl enable-linger $USER',
  }

  const steps = [
    <>
      <label>Name this computer
        <input autoFocus placeholder="macbook" value={name}
               onChange={(e) => setName(
                 e.target.value.replace(/[^\w.-]/g, '').toLowerCase())} />
      </label>
      <div className="cu-plat">
        {[['linux', 'Linux'], ['mac', 'macOS']].map(([k, l]) => (
          <button key={k} className={platform === k ? 'on' : ''}
                  onClick={() => setPlatform(k)}>{l}</button>
        ))}
      </div>
    </>,
    <>
      <label>Cloudflare Access token
        <span className="dim small">Only if Jarvis is behind Access. Kept in
          this page, never sent anywhere.</span>
        <input placeholder="Client Id" value={cfId}
               onChange={(e) => setCfId(cleanToken(e.target.value))} />
        <input type="password" placeholder="Client Secret" value={cfSecret}
               onChange={(e) => setCfSecret(cleanToken(e.target.value))} />
      </label>
    </>,
    <>
      <p>Run on <strong>{name}</strong>:</p>
      <Block text={cmds.fetch} />
    </>,
    <>
      <p>Check what it can drive:</p>
      <Block text={cmds.check} />
      <p className="dim small">Ends with the install commands for anything
        missing. Run those, then re-run this.</p>
      <p>Then start it:</p>
      <Block text={cmds.run} />
      {here
        ? <p className="badge">✓ {name} connected</p>
        : <p className="dim small">This updates the moment it connects.</p>}
    </>,
    <>
      <p>Save the settings:</p>
      <Block text={cmds.install} />
      <p>Then keep it running:</p>
      <Block text={cmds.enable} />
    </>,
  ]

  return (
    <div className="cu-scrim" onClick={onClose}>
      <div className="cu-modal" onClick={(e) => e.stopPropagation()}>
        <div className="cu-modal-head">
          <strong>Connect a computer</strong>
          <span className="grow" />
          <button className="ghost" onClick={onClose}>✕</button>
        </div>
        <ol className="cu-crumbs">
          {STEPS.map((s, i) => (
            <li key={s} className={i === step ? 'on' : i < step ? 'done' : ''}>
              {s}</li>
          ))}
        </ol>
        <div className="cu-modal-body">{steps[step]}</div>
        <div className="cu-modal-foot">
          <button className="ghost" disabled={!step}
                  onClick={() => setStep(step - 1)}>Back</button>
          <span className="grow" />
          {step < STEPS.length - 1
            ? <button disabled={!name} onClick={() => setStep(step + 1)}>Next</button>
            : <button onClick={onClose}>Done</button>}
        </div>
      </div>
    </div>
  )
}
