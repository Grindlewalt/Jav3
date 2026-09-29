// The words on the Security and VMs pages that explain what a thing is or what
// a button will do, kept in one place so they are written once, read together
// and tested (src/__tests__/securityCopy.test.mjs). Pure data and functions:
// no React.
//
// House rules for this file: plain and specific, sentence case, no "leverage"
// or "seamless". Every sentence has to be true of the code it describes.

import { newSitesText, runsInText } from './boxes/logic.js'

// ---- one line under each tab's title (WEB-12) ----------------------------------
// The Security and VMs shells look the current tab up here and hand the line to
// <Page lede>. The Secrets tab is missing on purpose: its panel already opens
// with its own line.
export const SECURITY_LEDES = {
  '/security': 'Everything waiting on you: alerts, sites a box tried to reach, and '
    + 'requests to commit, install a package or run a service.',
  '/security/persistent': 'What is still running inside each box beyond the operating '
    + 'system: services you approved, code the agent left running, and anything unexpected.',
  '/security/network': 'Every site the boxes try to reach, what was decided, and the lists '
    + "behind it. A site no list covers is blocked and waits here, unless the project's "
    + 'profile lets new sites through.',
  '/security/profiles': 'A profile is a saved set of security settings: what a project\'s '
    + 'boxes may reach on the network, which secrets it may use, and where it runs. '
    + 'Every project uses exactly one.',
  '/security/logs': 'Transcripts of every conversation, down to each tool call, and '
    + 'what the model calls cost.',
}

export const VMS_LEDES = {
  '/vms': 'Every box Jav3 runs: the shared box that chat turns use, project boxes, '
    + 'service boxes and image builders. A box is a virtual machine or, where the server '
    + 'allows it, a Docker container, which is less isolated.',
  '/vms/images': 'The disk images boxes start from. A variant adds packages on top of '
    + 'the base image; a box picks up a new version the next time it boots.',
  '/vms/catalogue': 'The history of package requests: what agents asked to install, '
    + 'what you decided, and which image each package went into.',
}

// The lede for a pathname, or '' for a tab with none. Exact match only (a
// trailing slash is ignored): the queue's key is a prefix of every other tab,
// so a prefix fallback would hand its line to Secrets and to a typo.
export function ledeFor(table, pathname) {
  const path = String(pathname || '').replace(/\/+$/, '') || '/'
  return Object.prototype.hasOwnProperty.call(table, path) ? table[path] : ''
}

// ---- waiting for you: egress requests (WEB-07) -----------------------------------

export const WAITING_LEDE = 'Sites a box tried to reach that no list covers yet. '
  + '"Allow always" puts the site on that project\'s always-allow list until you remove '
  + 'it. "Allow 1 h" lets it through for an hour and writes no list. Deny keeps it blocked.'

export const ALLOW_ALWAYS_TIP = (label) => (label
  ? `Add this site to ${label}'s always-allow list, for good. Remove it later under Always allow.`
  : "Add this site to the project's always-allow list, for good. You choose the project next.")

export const ALLOW_ONCE_TIP = 'Let this site through for one hour. It writes no list and '
  + 'comes back here if a box asks again after that.'

export const DENY_TIP = 'Keep this site blocked. It comes back here if a box asks again.'

// What a refused row says instead of offering Allow (WEB-08)
export const REFUSED_TAG = 'cannot be allowed'

// ---- the toast after a decision --------------------------------------------------

export function decidedText(host, verb, label) {
  if (verb === 'deny') return `${host} stays blocked`
  if (verb === 'once') return `${host} allowed for an hour${label ? ` for ${label}` : ''}`
  return `${host} added to ${label ? `${label}'s` : 'the'} always-allow list`
}

// ---- the project picker for a row that came from no project ---------------------

export const PICK_TITLE = (verb) => (verb === 'once'
  ? 'Which project may use it for an hour?' : 'Which project is this for?')

export const PICK_BODY = (host, verb) => `${host} was requested from the shared box, `
  + 'with no project attached. '
  + (verb === 'once'
    ? 'Pick the project that may reach it for the next hour.'
    : "Pick the project whose always-allow list it should go on.")

// ---- the Persistent tab's words (WEB-06) ------------------------------------------

export const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

export const PERSISTENT_LEGEND = [
  ['service', 'a service you approved, running as approved'],
  ['run_code', "left running by the agent's run_code tool"],
  ['unexpected', 'not on the box\'s baseline, and neither a service nor run_code'],
  ['stale', 'the box has not reported lately, so its list may be out of date'],
  ['built-in baseline', "no baseline was recorded for this box's image, so \"expected\" means "
    + 'a short built-in list of stock Debian services'],
  ['guest / host', 'guest is what the box says it sent (↑) and received (↓); host is what '
    + 'Jav3 measured itself, on the proxy or the port relay'],
  ['verified', 'guest and host numbers agree'],
  ['unverified', 'Jav3 has no measurement of its own for this connection'],
  ['mismatch', 'the two disagree by more than a few percent: the box may be misreporting'],
  ['unowned connection', 'the host saw a connection from the box that no reported process '
    + 'owns: something may be hiding from the box\'s process list'],
]

export const PERSISTENT_EMPTY_ODD = 'Nothing unexpected in any box.'
export const PERSISTENT_EMPTY_ALL = 'No box is reporting yet. Boxes appear here while they run.'
export const REPORTED_NEVER = 'has not reported yet'

// ---- allowing a process from its alert (WEB-02) ----------------------------------

// One entry of the operator's allowed list ({exe, unit, by, at}) as a line:
// exe "*" is a whole unit.
export function allowedLine(e) {
  const unit = e?.unit || ''
  const what = e?.exe === '*' ? `everything in ${unit}` : `${e?.exe}${unit ? ` in ${unit}` : ''}`
  const at = String(e?.at || '').slice(0, 10)
  return { what, when: [e?.by, at].filter(Boolean).join(' · ') }
}

const UNDO_WHERE = 'You can take it off again under Security, Persistent, "Allowed from alerts".'

// The confirm for "Allow this program" / "Allow the whole unit". `d` is the
// alert's detail ({exe, unit}); scope is 'program' | 'unit'.
export function baselineAsk(d, scope) {
  const exe = d?.exe || 'this program'
  const unit = d?.unit || ''
  if (scope === 'unit') {
    return {
      title: `Allow everything ${unit} runs?`,
      body: `${unit} joins the process baseline of every box, so nothing it starts is `
        + 'flagged as unexpected again, here or in the Persistent tab. Alerts already '
        + `waiting for it are cleared. Only do this for a system service you recognise. ${UNDO_WHERE}`,
      confirm: 'Allow the unit',
    }
  }
  return {
    title: `Allow ${exe}${unit ? ` in ${unit}` : ''}?`,
    body: 'It joins the process baseline of every box and stops alerting, here and in the '
      + 'Persistent tab. Alerts already waiting for it are cleared. '
      + `${unit ? `Other programs in ${unit} still alert. ` : ''}${UNDO_WHERE}`,
    confirm: 'Allow this program',
  }
}

// ---- the profile form's secret checklist (WEB-20) ---------------------------------

// `choices` is [{name, infrastructure}] from the host; `held` the names the
// profile already has. Infrastructure secrets (the Cloudflare Access token,
// Jav3's own credentials) stay out of the list until asked for, unless the
// profile already holds one, so it can be seen and unticked. Names the profile
// holds that no longer exist are listed too.
export function secretChecklist(choices, held, showInfra) {
  const infra = new Set(choices.filter((s) => s.infrastructure).map((s) => s.name))
  const names = [...new Set([...choices.map((s) => s.name), ...held])]
  const list = names.filter((n) => showInfra || !infra.has(n) || held.includes(n)).sort()
  const hidden = [...infra].filter((n) => !showInfra && !held.includes(n)).length
  return { list, infra, hidden }
}

// ---- Auto review (the queue's model reviewer) and Network's auto-allow (WEB-09) ---
// Two different features that both said "Auto": the reviewer sweeps the queue
// after the fact (backend/reviewer.py), the auto-allow judges a site the moment
// a box asks (backend/egress_auto.py). Named apart, each explained on screen.

export const AUTO_REVIEW_LEDE = [
  'A separate model with no tools reads new items every few minutes. It puts well-known '
    + "sites on their project's always-allow list and clears routine alerts; anything else "
    + 'it flags for you. It only touches projects whose profile allows it.',
  'It uses the model, so each run costs tokens, up to a cap per run. It never handles '
    + 'alerts about processes, secrets, or service and package requests. Everything it does '
    + 'is listed below, and each action can be undone.',
]

export const AUTO_REVIEW_ON_TIP = 'On: sweeps new items on its own every few minutes'
export const AUTO_REVIEW_OFF_TIP = 'Off: nothing is swept on its own; use Review now'

// "Last run: looked at 3 items, allowed 1 site, cleared 1 alert, flagged 1 for you"
export function tallyLine(last) {
  const n = last?.examined || 0
  return `looked at ${plural(n, 'item')}, allowed ${plural(last?.allowed || 0, 'site')}, `
    + `cleared ${plural(last?.acked || 0, 'alert')}, flagged ${last?.flagged || 0} for you`
    + (last?.error ? ' (stopped early)' : '')
}

// Network's switch: what it is called and what it does
export const AUTO_ALLOW_LABEL = 'Auto-allow new sites (experimental, can be wrong)'
export const AUTO_ALLOW_LEDE = 'When on, a site no list covers is judged the moment a box asks. '
  + 'Well-known sites are let through for 7 days, odd-looking ones are blocked, and the '
  + 'rest go to the model; a "not sure" waits for you. Different from Auto review on the '
  + 'Queue tab, which sweeps this list a few minutes later.'

// ---- agent reports (harness_fault) in the Queue (WEB-14) ---------------------------

export const FAULTS_LEDE = "Agents file these when one of Jav3's own tools misbehaved: an "
  + 'error on input that looked valid, or a call that did not do what it says. They are '
  + 'bug reports, not security alerts. Mark one resolved when you have dealt with it.'

// the row's text: the summary without the prefix the section title already says,
// and without the tool's name when the row shows it as a tag
export function faultText(summary, tool) {
  let t = String(summary || '').replace(/^Harness fault reported:\s*/, '')
  if (tool && t.startsWith(`${tool}: `)) t = t.slice(tool.length + 2)
  return t
}

// ---- questions an agent is waiting on in a chat (WEB-17) ----------------------------
// The nav badge counts them (they wait on you like everything else) but they are
// answered in the chat, so the Queue lists them with a link instead of leaving
// the badge one item ahead of the page.

export const ASKS_LEDE = 'An agent is waiting for your answer in these chats. Answer there: '
  + 'this list only points at them.'

export const askKindText = (kind) => (kind === 'permission' ? 'asks permission' : 'question')

export function askAge(seconds) {
  const s = Math.max(0, Number(seconds) || 0)
  if (s < 90) return 'just now'
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  if (s < 172800) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}

// ---- the VMs page: Docker boxes and the warning wall (WEB-11) ------------------------

// docker_runtime.NO_MEMORY_LIMIT: `docker run --memory` is ignored when the
// kernel has the memory cgroup off (Raspberry Pi OS ships that way)
const NO_MEMORY = /^no memory limits/i

export const dockerMemoryUnlimited = (runtimes) => !!runtimes?.docker?.available
  && (runtimes.docker.warnings || []).some((w) => NO_MEMORY.test(w))

// "512 MB", or, where the limit is not enforced, what is true
export const ramLabel = (memText, unlimited) =>
  (unlimited ? `no limit (${memText} not enforced)` : memText)

// The plain sentence that leads the runtime banner; the technical list goes
// under "Details". Empty when there is nothing to warn about.
export function dockerLead(docker) {
  if (!docker?.available || !(docker.weak || (docker.warnings || []).length)) return ''
  const parts = ['Docker boxes are less isolated than VMs: a container shares this '
    + "machine's kernel."]
  if ((docker.warnings || []).some((w) => NO_MEMORY.test(w))) {
    parts.push("On this machine they can also use all of its RAM, because the kernel has "
      + 'memory limits switched off.')
  }
  if (docker.weak) {
    parts.push('Docker also runs without user separation, so something that broke out of '
      + 'a container would have a real user\'s rights on the host.')
  }
  return parts.join(' ')
}

// ---- the Queue's "what is on" line (WEB-12) -------------------------------------------
// One item per default a newcomer cannot see anywhere else, each with the tab that
// changes it. An item whose data did not load is left out, never guessed.

export function postureItems({ profiles, auto, reviewer }) {
  const items = []
  const def = (profiles || []).find((p) => p.is_default)
  if (def) {
    const sites = newSitesText(def)
    items.push({
      label: 'Unlisted sites', value: sites, to: '/security/profiles',
      tone: sites === 'allowed' ? 'warn' : '',
      title: `What a box gets when it asks for a site no list covers, on the Default profile (${def.name}).`,
    })
    items.push({
      label: 'Projects run in', value: runsInText(def), to: '/security/profiles',
      title: `Where a project's boxes run unless it picks its own, on the Default profile (${def.name}).`,
    })
  }
  if (auto) {
    items.push({
      label: 'Auto-allow', value: auto.effective ? 'on' : 'off', to: '/security/network',
      tone: auto.effective ? 'warn' : '', title: AUTO_ALLOW_LEDE,
    })
  }
  if (reviewer) {
    items.push({
      label: 'Auto review', value: reviewer.enabled ? 'on' : 'off', to: '/security',
      title: AUTO_REVIEW_LEDE[0],
    })
  }
  return items
}

// ---- the Images tab (WEB-16) ------------------------------------------------------------
// A variant recipe's `from none` reaches the page as from: null. It is not
// "from nothing": it sits directly on the base image, and `main` is that image
// with nothing added, which is why it has no builds of its own.

export const variantSource = (v) => (v?.from ? `built on ${v.from}` : 'built directly on the base image')

export function variantBuilds(v, baseVersion) {
  if ((v?.versions || []).length) return null
  if (v?.name === 'main' && !(v.layer_packages || []).length) {
    return `runs the base image${baseVersion ? ` ${baseVersion}` : ''} with nothing added, so it has no builds of its own`
  }
  return 'never built'
}

export const NEEDS_BUILD_WHY = 'its recipe changed since the active version was built'

// ---- the Tools page (WEB-19) ------------------------------------------------------------

export const TOOLS_LEDE = 'What the agent can call. Yours are skills you wrote. Imported ones '
  + 'are skills brought in from elsewhere: each arrives off, and you grant it. Built-in ones '
  + 'ship with Jav3.'

export const IMPORT_TIP = 'Bring in a skill from a folder or a git address. It arrives '
  + 'switched off, and you grant it after reading what it asks for.'

// A built-in tool's state word. `enabled` is its folder's own switch; `offered`
// is whether the model is handed it right now (the host adds `reason` when not).
// An older host sends no `offered`: fall back to `enabled`.
export function builtinState(t) {
  const offered = t?.offered ?? !!t?.enabled
  if (offered) return { word: 'on', tone: 'done' }
  return t?.enabled === false ? { word: 'off', tone: '' } : { word: 'waiting', tone: 'pending' }
}

// ---- the browser tab's title (WEB-23) ------------------------------------------------------
// Every route used to be titled "Jav3", so a background tab could not say that
// approvals were waiting. `count` is the same number the Security nav link wears.

const TAB_NAMES = [
  ['/security/persistent', 'Security: Persistent'], ['/security/network', 'Security: Network'],
  ['/security/profiles', 'Security: Profiles'], ['/security/logs', 'Security: Logs'],
  ['/security/secrets', 'Security: Secrets'], ['/security', 'Security'],
  ['/vms/images', 'VMs: Images'], ['/vms/catalogue', 'VMs: Catalogue'], ['/vms', 'VMs'],
  ['/agents', 'Agents'], ['/tools', 'Tools'], ['/settings', 'Settings'], ['/memory', 'Memory'],
  ['/schedules', 'Schedules'], ['/shell', 'Shell'], ['/voice', 'Voice'],
  ['/artifacts', 'Artifacts'], ['/projects', 'Work'],
]

export function tabTitle(pathname, count = 0) {
  const path = String(pathname || '/').replace(/\/+$/, '') || '/'
  const hit = TAB_NAMES.find(([p]) => path === p || path.startsWith(`${p}/`))
  const name = hit ? hit[1] : (path === '/' || path.startsWith('/c/') ? 'Work' : 'Not found')
  const n = Number(count) > 0 ? `(${Number(count) > 99 ? '99+' : count}) ` : ''
  return `${n}${name} · Jav3`
}
