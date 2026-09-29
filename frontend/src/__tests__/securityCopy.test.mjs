// node frontend/src/__tests__/securityCopy.test.mjs
import assert from 'node:assert/strict'
import {
  ALLOW_ALWAYS_TIP, ALLOW_ONCE_TIP, PERSISTENT_LEGEND, SECURITY_LEDES, VMS_LEDES, WAITING_LEDE,
  allowedLine, baselineAsk, decidedText, ledeFor, plural,
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

// WEB-02: the confirm names the program and the unit, says how far it reaches and how to undo
const ask = baselineAsk({ exe: '/usr/local/bin/job', unit: 'weekly-job.service' }, 'program')
assert.equal(ask.title, 'Allow /usr/local/bin/job in weekly-job.service?')
assert.match(ask.body, /every box/)
assert.match(ask.body, /Other programs in weekly-job.service still alert/)
assert.match(ask.body, /Allowed from alerts/)
const unitAsk = baselineAsk({ exe: '/x', unit: 'weekly-job.service' }, 'unit')
assert.equal(unitAsk.title, 'Allow everything weekly-job.service runs?')
assert.match(unitAsk.body, /system service you recognise/)
assert.equal(baselineAsk({ exe: '/x' }, 'program').title, 'Allow /x?')
assert.deepEqual(allowedLine({ exe: '*', unit: 'a.service', by: 'grant', at: '2026-09-29 21:03:03' }),
  { what: 'everything in a.service', when: 'grant · 2026-09-29' })
assert.equal(allowedLine({ exe: '/x', unit: '' }).what, '/x')

// WEB-06: every tag the Persistent tab paints has a plain meaning, and plurals agree
const terms = PERSISTENT_LEGEND.map(([t]) => t)
for (const t of ['service', 'run_code', 'unexpected', 'stale', 'built-in baseline',
  'verified', 'unverified', 'mismatch']) assert.ok(terms.includes(t), t)
assert.equal(plural(1, 'box', 'boxes'), '1 box')
assert.equal(plural(0, 'box', 'boxes'), '0 boxes')
assert.equal(plural(2, 'alert'), '2 alerts')

console.log('securityCopy ok')
