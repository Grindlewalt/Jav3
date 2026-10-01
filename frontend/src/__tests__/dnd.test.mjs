// node frontend/src/__tests__/dnd.test.mjs
import assert from 'node:assert/strict'
import { dndBody, dndLeft, dndPresets, dndUntil, nextEight } from '../dnd.js'

// local-time dates: the end is the operator's 08:00, whatever zone this runs in
const at = (y, mo, d, h, mi = 0) => new Date(y, mo - 1, d, h, mi, 0, 0)

// before 08:00 it is this morning, after it is tomorrow's
assert.deepEqual(nextEight(at(2026, 10, 1, 2, 30)), at(2026, 10, 1, 8))
assert.deepEqual(nextEight(at(2026, 10, 1, 8, 0)), at(2026, 10, 2, 8))
assert.deepEqual(nextEight(at(2026, 10, 1, 23, 59)), at(2026, 10, 2, 8))

assert.equal(dndPresets(at(2026, 10, 1, 2)).find((p) => p.value === 'morning').label, 'Until 08:00')
assert.equal(dndPresets(at(2026, 10, 1, 14)).find((p) => p.value === 'morning').label,
  'Until tomorrow 08:00')
assert.deepEqual(dndPresets().map((p) => p.value), ['60', '240', 'morning', 'manual'])

const now = at(2026, 10, 1, 14)
assert.deepEqual(dndBody('off', now), { on: false })
assert.deepEqual(dndBody('manual', now), { on: true })
assert.deepEqual(dndBody('60', now), { on: true, minutes: 60 })
assert.deepEqual(dndBody('240', now), { on: true, minutes: 240 })
assert.equal(dndBody('morning', now).until, at(2026, 10, 2, 8).toISOString())
assert.equal(dndBody('nonsense', now), null)

assert.equal(dndUntil({ on: true, until: null }, now), 'until I turn it off')
assert.equal(dndUntil({ on: true, until: at(2026, 10, 1, 16, 5).toISOString() }, now), 'until 16:05')
assert.match(dndUntil({ on: true, until: at(2026, 10, 2, 8).toISOString() }, now), /^until \w+ 08:00$/)

assert.equal(dndLeft({ until: null }, now), '')
assert.equal(dndLeft({ until: at(2026, 10, 1, 14, 47).toISOString() }, now), '47m')
assert.equal(dndLeft({ until: at(2026, 10, 1, 15, 12).toISOString() }, now), '1h 12m')
assert.equal(dndLeft({ until: at(2026, 10, 1, 16, 0).toISOString() }, now), '2h')
assert.equal(dndLeft({ until: at(2026, 10, 1, 13, 0).toISOString() }, now), '0m')   // already over

console.log('dnd ok')
