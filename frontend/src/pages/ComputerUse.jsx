import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { Block } from '../copy.jsx'
import { TAB_ID, setTabName, tabName } from '../tab.js'
import { ClaimCard, useTicket } from './Pair.jsx'

// Everything that takes the client off a machine, in the order it has to happen.
//
// Written out by hand rather than calling `agent.py --uninstall`, and that is
// deliberate: the copy being removed is by definition the OLD one, and it may
// predate the flag. Every line is also safe to run when the thing it names is
// not there, so this is one paste whether the client was installed as a
// service, left running in a terminal, or half set up and abandoned.
//
// There is no button for this and there should not be. The client only ever
// accepts verbs from the closed table in backend/computeruse.py, and none of
// them can stop or uninstall it — a Jarvis that could remove itself from the
// operator's machines is a remote-kill primitive, and the whole design assumes
// Jarvis may be compromised. Ending access is the operator's own act, at their
// own terminal. Closing the process is all it takes; nothing listens here.
function removeCommands(platform) {
  const mac = platform === 'darwin' || platform === 'mac'
  return (mac
    ? 'launchctl bootout gui/$UID/network.atomos.jarvis.computeruse 2>/dev/null\n'
      + 'rm -f ~/Library/LaunchAgents/network.atomos.jarvis.computeruse.plist\n'
    : 'systemctl --user disable --now jarvis-computeruse.service 2>/dev/null\n'
      + 'rm -f ~/.config/systemd/user/jarvis-computeruse.service\n'
      + 'systemctl --user daemon-reload\n')
    // a client started by --setup runs in the foreground and has no service to
    // stop, so the paste has to cover that too or it looks like it worked and
    // the machine stays connected
    + 'pkill -f computeruse/agent.py 2>/dev/null\n'
    + 'rm -rf ~/jarvis-client\n'
    + 'rm -f ~/.config/jarvis/computeruse.json'
}

export default function ComputerUse() {
  const [state, setState] = useState(null)
  const [token, setToken] = useState('')
  const [open, setOpen] = useState(null)      // expanded machine
  const [probe, setProbe] = useState({})
  const [setupOpen, setSetupOpen] = useState(false)
  const [msg, setMsg] = useState(null)

  const refresh = () => api('/api/computeruse/status').then(setState)
  useEffect(() => {
    refresh()
    // the pairing token is only needed for the testing-only command, and only
    // while the Access secret is readable; fetched once so that path works
    api('/api/computeruse/token').then((r) => setToken(r.token)).catch(() => {})
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

      {/* The Access token, music server and Jellyfin forms moved to Settings:
          this tab is about machines, and three credential forms between the
          machines and the set-up button were three things to scroll past. */}
      <p className="dim small cu-settings-note">
        The Cloudflare Access token, music server and Jellyfin are on{' '}
        <Link to="/settings">Settings</Link>.
      </p>

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

          {/* Folded, because it is not the thing you came here for — but on the
              card, not buried in the set-up wizard, because "get this off my
              machine" is the one instruction you want to find in a hurry. */}
          <details className="cu-remove">
            <summary>Remove Jarvis from {m.name}</summary>
            <p className="dim small">
              Paste this into a terminal <strong>on {m.name}</strong>. It stops
              the client however it was started, removes its service definition,
              and deletes its folder and its saved pairing token. Every line is
              harmless if that part is already gone. {m.name} disappears from
              this page the moment the process ends.
            </p>
            <Block text={removeCommands(m.platform)} />
            <p className="dim small">
              Folders and privileges you granted {m.name} stay here, so setting
              it up again picks them straight back up. Remove them above if you
              want them gone. The pairing token is shared by every machine —
              rotating it disconnects all of them, so only do that if this one
              was compromised.
            </p>
          </details>
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
//
// The command carries a pairing code and nothing else. It used to carry the
// pairing token and the Cloudflare Access secret in plain text — the least
// secure part of the whole design, kept that way only for want of a channel.
// The channel is backend/pairing.py now: the machine claims the code, the
// operator confirms it (on the Confirm step here, or on /pair/CODE from any
// browser that is logged in), and the credentials go to the claiming process
// and nowhere else. The old command still exists, for testing a machine the
// pairing routes cannot reach, and only while the Settings page has made the
// secret readable.

const STEPS = ['Name', 'Set up', 'Confirm', 'Connected', 'Keep running']

// A path may contain a space, and one that does would otherwise arrive at the
// client as two --allow-root values, neither of which exists.
const shq = (s) => `'${String(s).replace(/'/g, "'\\''")}'`

function Setup({ token, machines, onClose }) {
  const [step, setStep] = useState(0)
  const [name, setName] = useState('')
  const [roots, setRoots] = useState('')
  const [err, setErr] = useState(null)
  const [made, setMade] = useState(null)         // the ticket as created
  const [cf, setCf] = useState(null)             // configured / revealed_until
  const [jumped, setJumped] = useState({})
  const [platform, setPlatform] = useState(
    () => (/Mac/.test(navigator.platform || navigator.userAgent) ? 'mac' : 'linux'))
  const [polled, gone, setTicket] = useTicket(made?.code)
  const ticket = polled || made

  // Whether the secret is readable decides which commands this can build, and
  // the Settings page can change that while this is open, so it is re-read
  // on every step rather than once.
  useEffect(() => {
    api('/api/computeruse/cfaccess').then(setCf).catch(() => setCf(null))
  }, [step])

  const origin = window.location.origin
  // A unique URL per time this dialog is opened, because the origin saying
  // "no-store" is not retroactive. Cloudflare caches this path by its .gz
  // extension and had already pinned a four-hour copy before the header
  // existed; that entry goes on being served until it expires no matter what
  // the origin says now. A query parameter is a different cache key, so it
  // misses the old entry entirely — and it keeps working if this is ever put
  // behind a CDN that ignores the header again.
  const [bust] = useState(() => Date.now().toString(36))
  const here = machines.find((m) => m.name === name)
  const rootList = roots.split(',').map((s) => s.trim()).filter(Boolean)
  const code = ticket?.code || ''
  const revealed = !!(cf?.revealed_until && cf.revealed_until > Date.now() / 1000)

  // "Next" on the first step is what makes the code, so the command on the
  // second step is real the moment it appears.
  async function makeCode() {
    setErr(null)
    try {
      const t = await api('/api/computeruse/enroll', {
        method: 'POST', body: JSON.stringify({ name }) })
      setMade(t)
      setTicket(t)
      setJumped({})
      setStep(1)
    } catch (e) { setErr(e.detail || String(e)) }
  }

  // The wizard follows the machine rather than waiting to be clicked: a claim
  // moves it to Confirm, and a connection moves it to Connected. The second
  // only from the Confirm step, so a machine of the same name that was already
  // connected does not skip the command.
  useEffect(() => {
    if (ticket?.state === 'claimed' && step === 1 && !jumped.claim) {
      setJumped((j) => ({ ...j, claim: true })); setStep(2)
    }
  }, [ticket?.state, step, jumped])
  useEffect(() => {
    if (here && step === 2 && !jumped.conn) {
      setJumped((j) => ({ ...j, conn: true })); setStep(3)
    }
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
  // So: && between every step so the first failure stops it, and --pair (or
  // --setup) at the end, which checks it can reach Jarvis, saves the settings,
  // says what is missing, and connects.
  const fetchLines = (url, headers) => [
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
    `  '${url}'`,
    ...headers.map(([k, v]) => `  -H '${k}: ${v}'`),
    `  )`,
    `  && { [ "$code" = 200 ] || { echo "the download answered HTTP $code, not 200:";`,
    `       head -c 300 c.tgz; echo;`,
    ...(headers.length ? [
      `       echo '  301/302 -> Cloudflare Access. This app needs its own Service';`,
      `       echo '             Auth policy naming your service token — policies are';`,
      `       echo '             per-application, so one that works for another host';`,
      `       echo '             does not cover this one.';`,
      `       echo '  401     -> Jarvis itself answered: the pairing token is stale.';`,
      `       echo '             Copy it again from the Computer use tab.';`,
    ] : [
      `       echo '  301/302 -> Cloudflare Access. The pairing routes are the one part';`,
      `       echo '             a new machine reaches with no credentials, so the Access';`,
      `       echo '             app needs a Bypass policy on /api/computeruse/pair/*.';`,
      `       echo '  401     -> the pairing code expired or was used. Make a new one';`,
      `       echo '             on the Computer use tab.';`,
    ]),
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
  ]
  const tail = `  || { rm -f c.tgz; echo 'set-up stopped — the error is above'; }`
  const py = '~/jarvis-client/.venv/bin/python'
  const cmds = {
    // the one to use: a code, and nothing that is a credential
    pair: [
      ...fetchLines(`${origin}/api/computeruse/pair/client.tar.gz?code=${code}&v=${bust}`, []),
      `  && .venv/bin/python computeruse/agent.py --pair ${code}`,
      `       --server ${origin}`,
      ...(name ? [`       --name ${name}`] : []),
      ...rootList.map((r) => `       --allow-root ${shq(r)}`),
      tail,
    ].join(' \\\n'),
    // testing only: the secrets inline, exactly as it used to be
    legacy: [
      ...fetchLines(`${origin}/api/computeruse/client.tar.gz?v=${bust}`, [
        ['X-Jarvis-Token', token],
        ...(cf?.secret ? [['CF-Access-Client-Id', cf.client_id],
                          ['CF-Access-Client-Secret', cf.secret]] : []),
      ]),
      `  && .venv/bin/python computeruse/agent.py --setup`,
      `       --server ${origin}`,
      `       --token ${token}`,
      ...(name ? [`       --name ${name}`] : []),
      ...rootList.map((r) => `       --allow-root ${shq(r)}`),
      ...(cf?.secret ? [`       --cf-access-id ${cf.client_id}`,
                        `       --cf-access-secret ${cf.secret}`] : []),
      tail,
    ].join(' \\\n'),
    // no flags: --pair/--setup already wrote them to ~/.config/jarvis/computeruse.json
    install: `${py} ~/jarvis-client/computeruse/agent.py --install`,
    // one source of truth with the card's own Remove section — a second copy of
    // this is a second thing to forget when a path changes
    remove: removeCommands(platform),
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
      {err && <p className="error">{err}</p>}
    </>,
    <>
      {gone ? (
        <>
          <p className="warn">This code has expired or been used.</p>
          <button onClick={makeCode}>Make a new code</button>
        </>
      ) : (
        <>
          <p>Paste this into a terminal on <strong>{name}</strong>:</p>
          <Block text={cmds.pair} />
          <p className="dim small">
            Pairing code <code className="pair-code">{code}</code>
            {ticket?.expires_in > 0 && ` · good for ${Math.ceil(ticket.expires_in / 60)} min`}
            {' '}· carries no secrets. The command downloads the client, gives it
            its own venv, claims the code and prints a confirm link; the next
            step here fills in the moment it does.
          </p>
        </>
      )}
      <details className="cu-remove">
        <summary>Already set one up on this machine?</summary>
        <p className="dim small">Run this first. It stops the old client, takes
          away its service definition, and deletes its folder and saved token —
          each line is harmless if that part is already gone.</p>
        <Block text={cmds.remove} />
      </details>
      <details className="cu-remove">
        <summary>The old command, secrets inline (testing only)</summary>
        {revealed ? (
          <>
            <p className="warn small">
              This carries the pairing token
              {cf?.secret ? ' and the Cloudflare Access secret' : ''} in plain
              text. Treat the terminal, its history and any screenshot as
              holding them. Readable for another{' '}
              {Math.ceil((cf.revealed_until - Date.now() / 1000) / 60)} min.
            </p>
            <Block text={cmds.legacy} />
          </>
        ) : (
          <p className="dim small">
            For a machine the pairing routes cannot reach. It is built only
            while the Access secret is readable, which is switched on from the
            danger zone at the bottom of <Link to="/settings">Settings</Link>.
          </p>
        )}
      </details>
    </>,
    <>
      {ticket && !gone
        ? <ClaimCard ticket={ticket} onChange={setTicket} compact />
        : <p className="warn">The code is no longer live — go back and make a new one.</p>}
      <p className="dim small">
        The same decision is at <code>{origin}/pair/{code}</code> from any
        browser that is logged in to Jarvis — that is the link the terminal
        prints.
      </p>
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
          <p className="dim small">Once confirmed, the client saves what it was
            handed, checks it can reach Jarvis, and connects; this fills in the
            moment it does. If it is still spinning, the terminal has the reason
            — a wrong address and a missing Cloudflare policy each say so by
            name.</p>
        </>
      )}
    </>,
    <>
      <p>Ctrl-C the client, then save what it is already using:</p>
      <Block text={cmds.install} />
      <p className="dim small">No flags — pairing saved them to
        ~/.config/jarvis/computeruse.json at 0600. The service definition gets
        the path, never the token.</p>
      <p>Then keep it running:</p>
      <Block text={cmds.enable} />
    </>,
  ]

  const next = () => (step === 0 ? makeCode() : setStep(step + 1))

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
            ? <button disabled={!name} onClick={next}>
                {step === 0 ? 'Make a code' : 'Next'}</button>
            : <button onClick={onClose}>Done</button>}
        </div>
      </div>
    </div>
  )
}
