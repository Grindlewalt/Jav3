// node frontend/src/__tests__/securityQueue.test.mjs
import assert from 'node:assert/strict'
import { liveInQueue } from '../securityQueue.js'

const ev = (extra) => ({ kind: 'write_flag', tier: 'alert', mode: 'badge', acknowledged: false, ...extra })

// a waiting alert, an approval, a critical cut: Queue items
assert.equal(liveInQueue(ev()), true)
assert.equal(liveInQueue(ev({ kind: 'service_requested', tier: 'approval', mode: 'ping' })), true)
assert.equal(liveInQueue(ev({ kind: 'egress_anomaly', tier: 'critical', mode: 'ping' })), true)

// an audit line (a known device's session, a device login) is history, not a Queue item
assert.equal(liveInQueue(ev({ kind: 'browser_session', tier: 'record', mode: 'record' })), false)
// ...unless the operator set that kind to Badge or Ping: then it counts, so it is listed
assert.equal(liveInQueue(ev({ kind: 'browser_session', tier: 'record', mode: 'badge' })), true)
assert.equal(liveInQueue(ev({ kind: 'browser_session', tier: 'record', mode: 'ping' })), true)

// filed already acknowledged (by you, by a rule, a Record-only kind): never
assert.equal(liveInQueue(ev({ acknowledged: true })), false)
assert.equal(liveInQueue(ev({ acknowledged: true, tier: 'critical' })), false)

// a critical row ignores the mode; an agent report keeps its section
assert.equal(liveInQueue(ev({ tier: 'critical', mode: 'record' })), true)
assert.equal(liveInQueue(ev({ kind: 'harness_fault', tier: 'record', mode: 'record' })), true)

// an event from a server that sends no mode: as it always was
assert.equal(liveInQueue({ kind: 'write_flag', tier: 'alert', acknowledged: false }), true)
assert.equal(liveInQueue(null), false)

console.log('securityQueue ok')
