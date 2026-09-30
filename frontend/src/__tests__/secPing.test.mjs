// node frontend/src/__tests__/secPing.test.mjs
import assert from 'node:assert/strict'
import { countsInBadge, isCrit, wantsPing } from '../secPing.js'

// the server's decision wins either way
assert.equal(wantsPing({ severity: 'warn', ping: true }), true)
assert.equal(wantsPing({ severity: 'critical', ping: false }), false)   // a rate-limited repeat
// no `ping` (an older server, a raise site that publishes itself): critical only
assert.equal(wantsPing({ severity: 'critical' }), true)
assert.equal(wantsPing({ severity: 'warn' }), false)
assert.equal(wantsPing({ severity: 'info' }), false)

// a warn-severity kind the server always treats as critical
assert.equal(isCrit({ severity: 'warn', tier: 'critical' }), true)
assert.equal(isCrit({ severity: 'CRITICAL' }), true)

// records stay out of the badge; repeats were already counted
assert.equal(countsInBadge({ tier: 'record', severity: 'info' }), false)
assert.equal(countsInBadge({ tier: 'approval', severity: 'info' }), true)   // a package request
assert.equal(countsInBadge({ tier: 'alert', severity: 'warn', repeat: true }), false)
assert.equal(countsInBadge({ severity: 'info' }), false)
assert.equal(countsInBadge({ severity: 'warn' }), true)

console.log('secPing ok')
