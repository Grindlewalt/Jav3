// The Notifications card's per-kind table, as pure functions. The server sends
// every kind with `chosen` (the operator set it) and `locked` (always pings);
// the card shows only the chosen ones until asked for more.

// the kinds the operator has changed; a locked kind has nothing to change
export const changedKinds = (kinds) => (kinds || []).filter((k) => k.chosen && !k.locked)

const plural = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`

// "73 kinds, 3 changed"
export function kindSummary(kinds) {
  const n = (kinds || []).length
  const c = changedKinds(kinds).length
  return `${plural(n, 'kind')}, ${c === 0 ? 'none' : c} changed`
}

// every word of the query appears in the kind's name; `_` reads as a space, so
// "site allowed" finds browser_site_allowed
export function matchKinds(kinds, query) {
  const words = String(query || '').toLowerCase().split(/[\s_]+/).filter(Boolean)
  if (!words.length) return kinds || []
  return (kinds || []).filter((k) => {
    const name = k.kind.toLowerCase().replace(/_/g, ' ')
    return words.every((w) => name.includes(w))
  })
}

const OTHER = 'other'

// The full list grouped by the word a kind starts with (browser_*, desk_*,
// persist_*). A prefix only one kind has would be a heading over a single row,
// so those fall together under "other", which comes last.
export function groupKinds(kinds) {
  const prefix = (k) => k.kind.split('_')[0]
  const size = new Map()
  for (const k of kinds || []) size.set(prefix(k), (size.get(prefix(k)) || 0) + 1)
  const groups = new Map()
  for (const k of kinds || []) {
    const p = size.get(prefix(k)) > 1 ? prefix(k) : OTHER
    if (!groups.has(p)) groups.set(p, [])
    groups.get(p).push(k)
  }
  const byName = (a, b) => a.kind.localeCompare(b.kind)
  return [...groups.entries()]
    .sort(([a], [b]) => (a === OTHER) - (b === OTHER) || a.localeCompare(b))
    .map(([name, list]) => ({ prefix: name, kinds: [...list].sort(byName) }))
}

// What the table shows. `all` is the "Show all" expansion (grouped), a query
// searches every kind, and with neither it is the changed ones. A search can
// find a kind that is not changed, so the operator can change one without
// opening the whole list.
export function visibleKinds(kinds, { all = false, query = '' } = {}) {
  const searching = String(query || '').trim() !== ''
  if (all) {
    // grouped on the whole list, then narrowed, so the headings do not
    // reshuffle as a query is typed
    const groups = groupKinds(kinds)
    if (!searching) return { grouped: true, groups }
    const keep = new Set(matchKinds(kinds, query).map((k) => k.kind))
    return { grouped: true, groups: groups
      .map((g) => ({ ...g, kinds: g.kinds.filter((k) => keep.has(k.kind)) }))
      .filter((g) => g.kinds.length) }
  }
  const list = searching ? matchKinds(kinds, query) : changedKinds(kinds)
  return { grouped: false, rows: list }
}
