// node frontend/src/__tests__/securityCopy.test.mjs
import assert from 'node:assert/strict'
import {
  ALLOW_ALWAYS_TIP, ALLOW_ONCE_TIP, SECURITY_LEDES, VMS_LEDES, WAITING_LEDE, decidedText, ledeFor,
} from '../securityCopy.js'

// WEB-12: every Security tab that has no line of its own gets one, found by path
for (const tab of ['/security', '/security/persistent', '/security/network',
  '/security/profiles', '/security/logs']) {
  const t = ledeFor(SECURITY_LEDES, tab)
  assert.ok(t.length > 30 && t.endsWith('.'), `lede for ${tab}`)
}
assert.equal(ledeFor(SECURITY_LEDES, '/security/network/'), SECURITY_LEDES['/security/network'])
// Secrets keeps its panel's own line; an unknown tab has none (the queue's key
// is a prefix of every other tab, and must not leak onto them)
assert.equal(ledeFor(SECURITY_LEDES, '/security/secrets'), '')
assert.equal(ledeFor(SECURITY_LEDES, '/security/bogus'), '')
assert.equal(ledeFor(SECURITY_LEDES, '/security/network/x'), '')
assert.notEqual(ledeFor(SECURITY_LEDES, '/security/logs'), SECURITY_LEDES['/security'])
assert.equal(ledeFor(SECURITY_LEDES, undefined), '')
for (const tab of ['/vms', '/vms/images', '/vms/catalogue']) {
  assert.ok(ledeFor(VMS_LEDES, tab).length > 30, `lede for ${tab}`)
}
assert.notEqual(ledeFor(VMS_LEDES, '/vms/images'), ledeFor(VMS_LEDES, '/vms'))

// WEB-07: the words say what Allow does, and that it is permanent for the project
assert.match(WAITING_LEDE, /always-allow list until you remove it/)
assert.match(WAITING_LEDE, /1 h/)
assert.match(ALLOW_ALWAYS_TIP('Snake'), /Snake's always-allow list, for good/)
assert.match(ALLOW_ALWAYS_TIP(''), /You choose the project next/)
assert.match(ALLOW_ONCE_TIP, /one hour/)
assert.equal(decidedText('pypi.org', 'deny'), 'pypi.org stays blocked')
assert.equal(decidedText('pypi.org', 'once', 'Snake'), 'pypi.org allowed for an hour for Snake')
assert.equal(decidedText('pypi.org', 'allow', 'Snake'), "pypi.org added to Snake's always-allow list")
assert.equal(decidedText('pypi.org', 'allow', ''), 'pypi.org added to the always-allow list')

console.log('securityCopy ok')
