// node frontend/src/__tests__/securityCopy.test.mjs
import assert from 'node:assert/strict'
import {
  ALLOW_ALWAYS_TIP, ALLOW_ONCE_TIP, AUTO_ALLOW_LABEL, AUTO_ALLOW_LEDE, AUTO_REVIEW_LEDE,
  PERSISTENT_LEGEND, SECURITY_LEDES, VMS_LEDES, WAITING_LEDE, allowedLine, baselineAsk,
  decidedText, ledeFor, plural, secretChecklist, tallyLine,
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

// WEB-20: infrastructure secrets are hidden from a profile's checklist until asked for,
// and stay visible when the profile already holds one
const choices = [{ name: 'NEWS_API_KEY', infrastructure: false },
  { name: 'CF_ACCESS_CLIENT_ID', infrastructure: true },
  { name: 'CF_ACCESS_CLIENT_SECRET', infrastructure: true }]
let cl = secretChecklist(choices, [], false)
assert.deepEqual(cl.list, ['NEWS_API_KEY'])
assert.equal(cl.hidden, 2)
cl = secretChecklist(choices, [], true)
assert.deepEqual(cl.list, ['CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET', 'NEWS_API_KEY'])
assert.equal(cl.hidden, 0)
cl = secretChecklist(choices, ['CF_ACCESS_CLIENT_ID', 'GONE_KEY'], false)
assert.deepEqual(cl.list, ['CF_ACCESS_CLIENT_ID', 'GONE_KEY', 'NEWS_API_KEY'])
assert.equal(cl.hidden, 1)
assert.ok(cl.infra.has('CF_ACCESS_CLIENT_ID'))

// WEB-09: Auto review says what it does, that it costs tokens and that it can be undone;
// the two "Auto" features have different names; the tally has no jargon
const lede = AUTO_REVIEW_LEDE.join(' ')
assert.match(lede, /no tools/)
assert.match(lede, /costs tokens/)
assert.match(lede, /can be undone/)
assert.match(lede, /always-allow list/)
assert.match(AUTO_ALLOW_LABEL, /experimental/)
assert.notEqual(AUTO_ALLOW_LABEL.toLowerCase().replace(/[^a-z]/g, ''), 'autoreview')
assert.match(AUTO_ALLOW_LEDE, /Different from Auto review/)
assert.equal(tallyLine({ examined: 3, allowed: 1, acked: 1, flagged: 1 }),
  'looked at 3 items, allowed 1 site, cleared 1 alert, flagged 1 for you')
assert.equal(tallyLine({ examined: 1, allowed: 0, acked: 2, flagged: 0, error: 'x' }),
  'looked at 1 item, allowed 0 sites, cleared 2 alerts, flagged 0 for you (stopped early)')
for (const jargon of ['unreviewed', 'acked', ' seen']) assert.ok(!lede.includes(jargon), jargon)

console.log('securityCopy ok')
