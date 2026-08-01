import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { TAB_ID, setTabName, tabName } from '../tab.js'

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
  const [cf, setCf] = useState({ configured: false, client_id: '', hosts: [] })
  const [cfId, setCfId] = useState('')
  const [cfSecret, setCfSecret] = useState('')
  const [cfResult, setCfResult] = useState(null)

  const refresh = () => api('/api/computeruse/status').then(setState)
  const loadCf = () => api('/api/computeruse/cfaccess')
    .then((r) => { setCf(r); setCfId(r.client_id || '') }).catch(() => {})
  useEffect(() => {
    refresh()
    api('/api/computeruse/token').then((r) => setToken(r.token)).catch(() => {})
    api('/api/computeruse/tarmac').then(setTm).catch(() => {})
    api('/api/computeruse/jellyfin').then(setJf).catch(() => {})
    loadCf()
    const t = setInterval(refresh, 6000)
    return () => clearInterval(t)
  }, [])

  // Rotating is the whole reason this panel exists, so it reports which
  // machines took the new token and which it could not reach — the second list
  // is the operator's remaining work, and leaving it out would imply the
  // rotation was complete when it was not.
  async function saveCf(e) {
    e.preventDefault()
    setCfResult(null)
    try {
      const r = await api('/api/computeruse/cfaccess', {
        method: 'PUT',
        body: JSON.stringify({ client_id: cfId, secret: cfSecret }) })
      setCfSecret('')
      setCfResult(r)
      loadCf()
    } catch (err) { setCfResult({ error: err.detail || String(err) }) }
  }

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
      await api('/api/computeruse/grants', {
        method: 'POST', body: JSON.stringify({ root, client }) })
      refresh()
      // and ask the machine what it made of it. A folder that exists here as a
      // string and not there as a directory is the commonest way to end up
      // being told there are no folders after adding one — the probe is what
      // turns that into a line of text next to the folder.
      runProbe(client)
      // No restart line any more: the host pushes the folder list to the
      // connected machine as part of this call. It used to say "restart the
      // client", which was true and useless — the restart meant re-running the
      // set-up command, so folders were the one setting this tab could not
      // actually change.
    } catch (err) { say(err.detail || String(err)) }
  }

  const revoke = async (id) => {
    await api(`/api/computeruse/grants/${id}`, { method: 'DELETE' })
    refresh()
  }

  // Only the URL now. The music server's Access token stopped being its own
  // thing — it is the one token, held above, and having a second copy here is
  // precisely how rotating it broke music while everything else looked fine.
  async function saveMusic(e) {
    e.preventDefault()
    setTmTest(null)
    try {
      setTm(await api('/api/computeruse/tarmac', {
        method: 'PUT', body: JSON.stringify({ url: tm.url }) }))
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
        <Machine key={m.id} m={m} caps={caps} served={state.served_version}
                 expanded={open === m.name}
                 onToggle={() => {
                   const opening = open !== m.name
                   setOpen(opening ? m.name : null)
                   // probe on open, not on a button: the folder health is the
                   // reason to open this at all
                   if (opening && !probe[m.name]) runProbe(m.name)
                 }}
                 probe={probe[m.name]} onProbe={() => runProbe(m.name)}
                 onPriv={togglePriv} onAdd={addFolder} onRevoke={revoke} />
      ))}

      <Tabs />

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
        <h2>Cloudflare Access token</h2>
        <form className="row" onSubmit={saveCf}>
          <input className="grow" placeholder="Client Id (ends in .access)"
                 value={cfId}
                 onChange={(e) => setCfId(cleanToken(e.target.value))} />
          <input type="password"
                 placeholder={cf.configured ? 'secret (stored)' : 'Client Secret'}
                 value={cfSecret}
                 onChange={(e) => setCfSecret(cleanToken(e.target.value))} />
          <button type="submit" disabled={!cfId || !cfSecret}>Save & push</button>
        </form>
        {cfResult && (cfResult.error
          ? <p className="error">{cfResult.error}</p>
          : <p className="badge">
              Saved.{' '}
              {cfResult.updated?.length
                ? `Pushed to ${cfResult.updated.join(', ')} — `
                  + 'each takes it on its next reconnect.'
                : 'No machine was connected to push it to.'}
              {cfResult.missed?.length
                ? ` Could not reach ${cfResult.missed.join(', ')}.` : ''}
            </p>)}
        <p className="dim small">
          One token, held here and used for everything: Jarvis, the music server,
          and the set-up command, which fills it in so you never type it. Saving
          a rotated one pushes it to every machine that is connected right now —
          a machine that is offline cannot be told, because Jarvis is behind the
          thing being rotated, so that one needs it pasted in once.
        </p>
      </section>

      <section className="panel">
        <h2>Music server</h2>
        <form className="row" onSubmit={saveMusic}>
          <input className="grow" placeholder="https://music.atomos.network"
                 value={tm.url}
                 onChange={(e) => setTm({ ...tm, url: e.target.value })} />
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
          It is a separate Cloudflare Access application, so the token above
          needs its own Service Auth policy there as well as on this one.
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

function Machine({ m, caps, served, expanded, onToggle, probe, onProbe,
                   onPriv, onAdd, onRevoke }) {
  const [root, setRoot] = useState('')
  const privs = m.privileges || {}
  const off = Object.values(privs).filter((v) => v === false).length
  const folders = m.grants || []
  // What that machine says about each granted folder. Keyed by path, because a
  // grant is a string here and a real directory (or not) over there — and the
  // gap between those two is exactly where "I added the folders" and "there are
  // no folders" were both true.
  const health = {}
  for (const r of (probe?.roots_detail || [])) health[r.path] = r
  const stale = served && m.version && m.version !== served

  return (
    <section className="panel cu-machine">
      <button className="cu-machine-head" onClick={onToggle}>
        <span className="run-dot running" />
        <span className="grow">
          <strong>{m.name}</strong>
          <span className="dim"> · {m.platform === 'darwin' ? 'macOS' : m.platform}</span>
          {m.caps?.dry_run && <span className="tag">dry run</span>}
          {/* A stale client and a broken one look identical from here, and the
              difference has cost two evenings — a CDN pinned an old download
              twice. Now it says so. */}
          {stale && <span className="tag warn" title={
            `running build ${m.version || 'unknown'}, this Jarvis serves ${served}`
          }>old build — re-run set-up</span>}
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
                {folders.map((g) => {
                  const h = health[g.root]
                  return (
                    <li key={g.id}>
                      <code className="grow">{g.root}</code>
                      {!g.client && <span className="tag">all computers</span>}
                      {h && h.ok && (
                        <span className="dim small">
                          {h.audio} audio · {h.video} video</span>)}
                      {h && !h.ok && (
                        <span className="warn small" title={h.why}>{h.why}</span>)}
                      <button className="ghost danger"
                              onClick={() => onRevoke(g.id)}>remove</button>
                    </li>
                  )
                })}
              </ul>
            )}
          {probe?.grant_note && (
            <p className="warn small">{probe.grant_note}</p>)}
          {probe && !probe.loading && !probe.error
            && Array.isArray(probe.binaries) && !probe.binaries.includes('mpv') && (
            <p className="warn small">
              mpv is not installed on this computer, so it can play nothing from
              disk. <code>brew install mpv</code> on a Mac, then restart the
              client.
            </p>
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

// --- open Jarvis tabs ---------------------------------------------------------
//
// The other kind of "computer" Jarvis can put sound on: a browser with Jarvis
// open. Music used to start in ALL of them at once because none had a name and
// there was nothing to address. They have names now, so this shows them and
// lets you change what this one is called — which is what you then say to
// Jarvis ("put it on the mac").

function Tabs() {
  const [tabs, setTabs] = useState([])
  const [name, setName] = useState(tabName())
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    const load = () => api('/api/gui/tabs').then((r) => setTabs(r.tabs || []))
                          .catch(() => {})
    load()
    const t = setInterval(load, 6000)
    return () => clearInterval(t)
  }, [])

  function save(e) {
    e.preventDefault()
    setTabName(name)
    setSaved(true)
    setTimeout(() => setSaved(false), 4000)
  }

  return (
    <section className="panel">
      <h2>Open Jarvis tabs</h2>
      <p className="dim small">
        Music and video play in ONE of these — the tab you asked from, unless you
        name another. Say the name to Jarvis.
      </p>
      {tabs.length === 0
        ? <p className="dim small">none reporting yet</p>
        : (
          <ul className="cu-grants">
            {tabs.map((t) => (
              <li key={t.id}>
                <span className="grow">{t.name}</span>
                {t.id === TAB_ID && <span className="tag">this one</span>}
              </li>
            ))}
          </ul>
        )}
      <form className="row" onSubmit={save}>
        <input className="grow" value={name} maxLength={60}
               onChange={(e) => setName(e.target.value)}
               placeholder="what to call this browser" />
        <button type="submit" disabled={!name.trim()}>Rename this tab</button>
      </form>
      {saved && (
        <p className="dim small">
          Saved. It takes the new name when this tab next reconnects — reload to
          do that now.
        </p>
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

const STEPS = ['Name', 'Access', 'Set up', 'Connected', 'Keep running']

// A path may contain a space, and one that does would otherwise arrive at the
// client as two --allow-root values, neither of which exists.
const shq = (s) => `'${String(s).replace(/'/g, "'\\''")}'`

function Setup({ token, machines, onClose }) {
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [roots, setRoots] = useState('')
  const [cfId, setCfId] = useState('')
  const [cfSecret, setCfSecret] = useState('')
  const [cfFromStore, setCfFromStore] = useState(false)
  const [jumped, setJumped] = useState(false)
  const [platform, setPlatform] = useState(
    () => (/Mac/.test(navigator.platform || navigator.userAgent) ? 'mac' : 'linux'))

  // The token is stored once, host-side, so setting up the fifth machine does
  // not mean finding it in Zero Trust for the fifth time. Typing over it still
  // works — the field is prefilled, not locked.
  useEffect(() => {
    api('/api/computeruse/cfaccess').then((r) => {
      if (!r.configured) return
      setCfId(r.client_id)
      setCfSecret(r.secret)
      setCfFromStore(true)
    }).catch(() => { /* not behind Access, or never configured */ })
  }, [])

  const origin = window.location.origin
  // A unique URL per time this dialog is opened, because the origin saying
  // "no-store" is not retroactive. Cloudflare caches this path by its .gz
  // extension and had already pinned a four-hour copy before the header
  // existed; that entry goes on being served until it expires no matter what
  // the origin says now. A query parameter is a different cache key, so it
  // misses the old entry entirely — and it keeps working if this is ever put
  // behind a CDN that ignores the header again.
  const [bust] = useState(() => Date.now().toString(36))
  const behind = !!(cfId && cfSecret)
  const here = machines.find((m) => m.name === name)
  const rootList = roots.split(',').map((s) => s.trim()).filter(Boolean)

  // It arrives connected or it does not arrive: the set-up step is one paste
  // that ends with the client running, so the moment it lands the wizard should
  // be showing what landed rather than waiting to be clicked forward.
  useEffect(() => {
    if (here && step === 2 && !jumped) { setJumped(true); setStep(3) }
  }, [here, step, jumped])

  // One chained command, on purpose. Every line of it used to be a step the
  // operator ran by hand, and each one had a way to fail that left set-up half
  // done with no sign of it:
  //   - `unzip` is not in a base Linux install; tar is. The zip download
  //     succeeded and then died on `unzip: command not found`, and the next
  //     line ran anyway against a directory that was never unpacked.
  //   - `pip install` into the system python is refused outright on Arch and
  //     Debian (PEP 668), and into whatever venv happened to be active it puts
  //     the deps somewhere the service will never look.
  //   - Starting the client before its settings were saved just printed the
  //     usage message: --server and --token were only saved by --install, which
  //     came a step later.
  // So: && between every step so the first failure stops it, and --setup at the
  // end, which checks it can reach Jarvis, saves the settings, says what is
  // missing, and connects.
  const py = '~/jarvis-client/.venv/bin/python'
  const cmds = {
    setup: [
      `mkdir -p ~/jarvis-client && cd ~/jarvis-client`,
      // Two changes from `curl -fsSL`, both of which cost real debugging time:
      //
      //   -f prints NOTHING on an HTTP error — no status, no body — so every
      //   refusal looked the same and named nothing. -w '%{http_code}' keeps it.
      //
      //   -L silently FOLLOWED Cloudflare Access's 302 to its login page, which
      //   answers 200 with HTML. The status check passed, and the operator got
      //   "gzip: stdin: not in gzip format" from tar — an error about archives
      //   for what is actually an authentication problem. Redirects are not
      //   followed now, so a 302 is reported as a 302, and the gzip test below
      //   catches an HTML page that arrives with a 200 anyway (a WAF block page
      //   does exactly that).
      // -A names this request. curl's own user-agent happens to be allowed
      // today, but the very next step of set-up was refused with a 403 purely
      // for sending "Python-urllib/3.x", so the lesson is that an anonymous
      // request is a bot-rule away from failing. Both halves say the same name.
      `  && code=$(curl -sS -o c.tgz -w '%{http_code}' -A 'jarvis-computeruse/1.0'`,
      `  '${origin}/api/computeruse/client.tar.gz?v=${bust}'`,
      `  -H 'X-Jarvis-Token: ${token}'`,
      ...(behind ? [`  -H 'CF-Access-Client-Id: ${cfId}'`,
                    `  -H 'CF-Access-Client-Secret: ${cfSecret}'`] : []),
      `  )`,
      `  && { [ "$code" = 200 ] || { echo "the download answered HTTP $code, not 200:";`,
      `       head -c 300 c.tgz; echo;`,
      `       echo '  301/302 -> Cloudflare Access. This app needs its own Service';`,
      `       echo '             Auth policy naming your service token — policies are';`,
      `       echo '             per-application, so one that works for another host';`,
      `       echo '             does not cover this one.';`,
      `       echo '  401     -> Jarvis itself answered: the pairing token is stale.';`,
      `       echo '             Copy it again from the Computer use tab.';`,
      `       echo '  403     -> something in FRONT of Jarvis refused it. Jarvis never';`,
      `       echo '             answers 403 here, so look at a WAF rule, Bot Fight';`,
      `       echo '             Mode (it blocks curl by user-agent), or Access.';`,
      `       rm -f c.tgz; false; }; }`,
      `  && { gzip -t c.tgz 2>/dev/null || { echo 'that answered 200 but is not a tarball:';`,
      `       head -c 300 c.tgz; echo;`,
      `       echo 'HTML here means a login or block page replied instead of Jarvis.';`,
      `       rm -f c.tgz; false; }; }`,
      `  && tar xzf c.tgz && rm -f c.tgz`,
      `  && python3 -m venv .venv`,
      `  && .venv/bin/pip install -q -r computeruse/requirements.txt`,
      `  && .venv/bin/python computeruse/agent.py --setup`,
      `       --server ${origin}`,
      `       --token ${token}`,
      ...(name ? [`       --name ${name}`] : []),
      ...rootList.map((r) => `       --allow-root ${shq(r)}`),
      ...(behind ? [`       --cf-access-id ${cfId}`,
                    `       --cf-access-secret ${cfSecret}`] : []),
      `  || { rm -f c.tgz; echo 'set-up stopped — the error is above'; }`,
    ].join(' \\\n'),
    // no flags: --setup already wrote them to ~/.config/jarvis/computeruse.json
    install: `${py} ~/jarvis-client/computeruse/agent.py --install`,
    // Written out by hand rather than calling agent.py --uninstall, because the
    // copy being removed is by definition the OLD one and may predate that
    // flag. Every line is safe to run when the thing it names is not there.
    remove: (platform === 'mac'
      ? 'launchctl bootout gui/$UID/network.atomos.jarvis.computeruse 2>/dev/null\n'
        + 'rm -f ~/Library/LaunchAgents/network.atomos.jarvis.computeruse.plist\n'
      : 'systemctl --user disable --now jarvis-computeruse.service 2>/dev/null\n'
        + 'rm -f ~/.config/systemd/user/jarvis-computeruse.service\n'
        + 'systemctl --user daemon-reload\n')
      + 'rm -rf ~/jarvis-client\n'
      + 'rm -f ~/.config/jarvis/computeruse.json',
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
      <label>Folders it may play from
        <span className="dim small">Optional — you can add and remove folders
          from this page afterwards and the client picks them up straight away,
          no restart. Comma separated.</span>
        <input placeholder={platform === 'mac'
                 ? '~/Music, ~/Movies' : '~/Music, ~/Videos'}
               value={roots} onChange={(e) => setRoots(e.target.value)} />
      </label>
    </>,
    <>
      <label>Cloudflare Access token
        <span className="dim small">
          {cfFromStore
            ? 'Filled in from the one Jarvis holds — nothing to type. Change it '
              + 'here to use a different token for just this machine; to rotate '
              + 'it everywhere, use the Access token panel on the Computer use '
              + 'tab instead.'
            : 'Only if Jarvis is behind Access. Save it on the Computer use tab '
              + 'and it will be filled in here from then on.'}
        </span>
        <input placeholder="Client Id" value={cfId}
               onChange={(e) => { setCfId(cleanToken(e.target.value)); setCfFromStore(false) }} />
        <input type="password" placeholder="Client Secret" value={cfSecret}
               onChange={(e) => { setCfSecret(cleanToken(e.target.value)); setCfFromStore(false) }} />
      </label>
    </>,
    <>
      <p>Paste this into a terminal on <strong>{name}</strong>:</p>
      <Block text={cmds.setup} />
      <p className="dim small">Downloads the client, gives it its own venv,
        checks it can reach Jarvis, lists anything missing with the install
        command for <em>this</em> machine, then connects and stays in the
        foreground.</p>
      <details className="cu-remove">
        <summary>Already set one up on this machine?</summary>
        <p className="dim small">Run this first. It stops the old client, takes
          away its service definition, and deletes its folder and saved token —
          each line is harmless if that part is already gone.</p>
        <Block text={cmds.remove} />
      </details>
    </>,
    <>
      {here ? (
        <>
          <p className="badge">✓ {here.name} connected
            <span className="dim"> · {here.platform === 'darwin'
              ? 'macOS' : here.platform}</span></p>
          <Hardware d={here.caps || {}} />
          <p className={(here.grants || []).length ? 'dim small' : 'warn'}>
            {(here.grants || []).length
              ? `${here.grants.length} folder${here.grants.length === 1 ? '' : 's'} granted`
              : 'No folders granted yet, so nothing on it can be played — add '
                + 'one from its card on this page.'}
          </p>
        </>
      ) : (
        <>
          <p>Waiting for <strong>{name || 'the client'}</strong>…</p>
          <p className="dim small">The command ends by connecting, and this
            fills in the moment it does. If it is still spinning, the terminal
            has the reason — a wrong address, a rotated token and a missing
            Cloudflare service token each say so by name.</p>
        </>
      )}
    </>,
    <>
      <p>Ctrl-C the client, then save what it is already using:</p>
      <Block text={cmds.install} />
      <p className="dim small">No flags — set-up saved them to
        ~/.config/jarvis/computeruse.json at 0600. The service definition gets
        the path, never the token.</p>
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
