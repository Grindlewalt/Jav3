// The words on the Security and VMs pages that explain what a thing is or what
// a button will do, kept in one place so they are written once, read together
// and tested (src/__tests__/securityCopy.test.mjs). Pure data and functions:
// no React.
//
// House rules for this file: plain and specific, sentence case, no "leverage"
// or "seamless". Every sentence has to be true of the code it describes.

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
    + 'service boxes and image builders. Some run as virtual machines, some as Docker '
    + 'containers, which are less isolated.',
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
