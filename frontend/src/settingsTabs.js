// Settings is four tabs, each a real URL (/settings, /settings/alerts, ...).
// This is the map between them and the older links into the page: every card
// used to live on one long page, so the world links to /settings#desk,
// /settings#notifications, #providers. Pure, so a node test covers it.

export const SETTINGS_TABS = [
  { id: 'models', to: '/settings', label: 'Models', end: true },
  { id: 'alerts', to: '/settings/alerts', label: 'Alerts' },
  { id: 'access', to: '/settings/access', label: 'Access' },
  { id: 'system', to: '/settings/system', label: 'System' },
]

// the card ids (and the tab names themselves) a #hash may name -> the tab
// that now holds that card
const HASH_TAB = {
  models: 'models', providers: 'models',
  alerts: 'alerts', notifications: 'alerts',
  access: 'access', devices: 'access', desk: 'access', browser: 'access',
  rules: 'access', permissions: 'access', grounding: 'access',
  system: 'system', backup: 'system', music: 'system', session: 'system',
}

// the card id a #hash names
export const hashId = (hash) => String(hash || '').replace(/^#/, '').toLowerCase()

export const tabPath = (id) => (SETTINGS_TABS.find((t) => t.id === id) || SETTINGS_TABS[0]).to

// the tab a pathname shows; /settings itself (the index) is Models
export function tabForPath(pathname) {
  const m = /^\/settings\/([^/]+)\/?$/.exec(pathname || '')
  const hit = m && SETTINGS_TABS.find((t) => t.id === m[1])
  return hit ? hit.id : 'models'
}

// the tab that holds the card a #hash names, or null for a hash that names
// nothing here
export const tabForHash = (hash) => HASH_TAB[hashId(hash)] || null

// Where to go instead, when a link names a card on a different tab than the
// one it landed on: `/settings#notifications` -> `/settings/alerts#notifications`.
// null when the link is already on the right tab (or the hash is not ours).
export function settingsRedirect(pathname, hash) {
  const want = tabForHash(hash)
  if (!want || want === tabForPath(pathname)) return null
  return `${tabPath(want)}${hash.startsWith('#') ? hash : `#${hash}`}`
}
