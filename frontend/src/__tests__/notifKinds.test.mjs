// node frontend/src/__tests__/notifKinds.test.mjs
import assert from 'node:assert/strict'
import { changedKinds, groupKinds, kindSummary, matchKinds, visibleKinds } from '../notifKinds.js'

// the server's list: every kind, `chosen` when the operator set it, `locked` when it always pings
const k = (kind, extra = {}) => ({ kind, usual: 'info', chosen: false, locked: false,
  mode: 'record', default: 'record', ...extra })
const kinds = [
  k('browser_site_allowed', { chosen: true, mode: 'ping' }),
  k('browser_site_denied'),
  k('browser_killed', { chosen: true, mode: 'badge' }),
  k('desk_session'),
  k('desk_killed'),
  k('login_failed'),
  k('host_cut', { locked: true, mode: 'ping', default: 'ping' }),
  k('secret_leak', { locked: true, chosen: true }),      // locked: nothing to change
  k('persist_approved', { chosen: true }),
]

// the changed ones, in the order given; a locked kind is never "changed"
assert.deepEqual(changedKinds(kinds).map((x) => x.kind),
  ['browser_site_allowed', 'browser_killed', 'persist_approved'])
assert.deepEqual(changedKinds([]), [])
assert.deepEqual(changedKinds(undefined), [])

assert.equal(kindSummary(kinds), '9 kinds, 3 changed')
assert.equal(kindSummary(kinds.filter((x) => !x.chosen)), '5 kinds, none changed')
assert.equal(kindSummary([k('a_b', { chosen: true })]), '1 kind, 1 changed')
assert.equal(kindSummary([]), '0 kinds, none changed')

// search: every word, any order, `_` is a space, case does not matter
const names = (list) => list.map((x) => x.kind)
assert.deepEqual(names(matchKinds(kinds, 'killed')), ['browser_killed', 'desk_killed'])
assert.deepEqual(names(matchKinds(kinds, 'site allowed')), ['browser_site_allowed'])
assert.deepEqual(names(matchKinds(kinds, 'ALLOWED site')), ['browser_site_allowed'])
assert.deepEqual(names(matchKinds(kinds, 'browser_site')),
  ['browser_site_allowed', 'browser_site_denied'])
assert.deepEqual(matchKinds(kinds, 'zzz'), [])
assert.equal(matchKinds(kinds, '  ').length, kinds.length)       // blank is no filter
assert.equal(matchKinds(kinds, '').length, kinds.length)

// groups by first word; a word only one kind starts with falls under "other", last
const groups = groupKinds(kinds)
assert.deepEqual(groups.map((g) => g.prefix), ['browser', 'desk', 'other'])
assert.deepEqual(names(groups[0].kinds), ['browser_killed', 'browser_site_allowed', 'browser_site_denied'])
assert.deepEqual(names(groups[2].kinds), ['host_cut', 'login_failed', 'persist_approved', 'secret_leak'])
assert.equal(groups.reduce((n, g) => n + g.kinds.length, 0), kinds.length)   // none lost
assert.deepEqual(groupKinds([]), [])

// what the table shows: the changed ones, a search of every kind, or all of them grouped
assert.deepEqual(visibleKinds(kinds, {}), { grouped: false, rows: changedKinds(kinds) })
assert.deepEqual(names(visibleKinds(kinds, { query: 'desk' }).rows), ['desk_session', 'desk_killed'])
assert.deepEqual(visibleKinds(kinds, { query: 'zzz' }), { grouped: false, rows: [] })
assert.deepEqual(visibleKinds(kinds, { query: '  ' }), { grouped: false, rows: changedKinds(kinds) })
const all = visibleKinds(kinds, { all: true })
assert.equal(all.grouped, true)
assert.equal(all.groups.reduce((n, g) => n + g.kinds.length, 0), kinds.length)
// a query in the full list keeps the headings the whole list has
const narrowed = visibleKinds(kinds, { all: true, query: 'killed' }).groups
assert.deepEqual(narrowed.map((g) => g.prefix), ['browser', 'desk'])
assert.deepEqual(narrowed.map((g) => names(g.kinds)), [['browser_killed'], ['desk_killed']])
assert.deepEqual(visibleKinds(kinds, { all: true, query: 'zzz' }), { grouped: true, groups: [] })

console.log('notifKinds ok')
