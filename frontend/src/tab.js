// Which machine this browser tab is, so Jarvis can play something HERE.
//
// Every open Jarvis tab used to be an anonymous subscriber on one broadcast
// channel, so asking for music started it on the laptop, the desktop and the
// phone at once — all slightly out of sync, with no way to say which one you
// meant because none of them had a name. A tab now carries:
//
//   id    unique per tab, kept in sessionStorage so a reload is the same tab
//         and a second window is a different one
//   name  what the operator would call this machine, kept in localStorage so
//         it is per browser rather than per tab, and renameable on the
//         Computer use tab
//
// Neither is a credential: the session cookie is still what authenticates, and
// every tab belongs to the same logged-in operator. This is addressing, not
// authorisation.

const ID_KEY = 'jarvis.tab.id'
const NAME_KEY = 'jarvis.tab.name'

function makeId() {
  try {
    return crypto.randomUUID().slice(0, 12)
  } catch {
    return Math.random().toString(36).slice(2, 14)
  }
}

function read(store, key) {
  try { return store.getItem(key) || '' } catch { return '' }   // private mode
}

function write(store, key, value) {
  try { store.setItem(key, value) } catch { /* nothing to do about it */ }
}

let id = read(sessionStorage, ID_KEY)
if (!id) {
  id = makeId()
  write(sessionStorage, ID_KEY, id)
}

export const TAB_ID = id

// A first guess at the name, so the tab list is readable before anyone renames
// anything. iPadOS reports itself as a Mac, hence the touch check — an iPad
// called "Mac" in a device list is the kind of small lie that costs a minute
// every time someone reads it.
function guess() {
  const ua = navigator.userAgent || ''
  const touch = navigator.maxTouchPoints > 1
  const os = /iPhone/.test(ua) ? 'iPhone'
    : /iPad/.test(ua) || (/Macintosh/.test(ua) && touch) ? 'iPad'
    : /Android/.test(ua) ? 'Android'
    : /Macintosh|Mac OS/.test(ua) ? 'Mac'
    : /Windows/.test(ua) ? 'Windows'
    : /Linux/.test(ua) ? 'Linux' : 'browser'
  const browser = /Edg\//.test(ua) ? 'Edge'
    : /OPR\//.test(ua) ? 'Opera'
    : /Firefox\//.test(ua) ? 'Firefox'
    : /Chrome\//.test(ua) ? 'Chrome'
    : /Safari\//.test(ua) ? 'Safari' : ''
  return browser ? `${os} · ${browser}` : os
}

export function tabName() {
  return read(localStorage, NAME_KEY) || guess()
}

export function setTabName(name) {
  const clean = String(name || '').trim().slice(0, 60)
  write(localStorage, NAME_KEY, clean)
  // the name travels on the SSE subscription, so it only takes effect when
  // that reconnects — say so rather than leaving the old name showing
  window.dispatchEvent(new CustomEvent('jarvis-tab-renamed', { detail: clean }))
  return clean || guess()
}

export function streamUrl() {
  return `/api/gui/stream?tab=${encodeURIComponent(TAB_ID)}`
       + `&name=${encodeURIComponent(tabName())}`
}
