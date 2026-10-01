// node frontend/src/__tests__/securityRuns.test.mjs
import assert from 'node:assert/strict'
import {
  UNTRUSTED, cardTotals, cardsFor, chatHref, clock, countsLine, countsParts, eventActions,
  kindsToMute, lineTitle, muteAsk, mutable, programOf, runState, stopRunAsk, subjectsText,
} from '../securityRuns.js'

// the header's counts, as the sketch words them
assert.equal(countsLine({ need: 2, filtered: 47, reports: 1 }),
  '2 need you · 47 filtered as normal work · 1 agent report')
assert.equal(countsLine({ need: 1, filtered: 0, reports: 3 }), '1 need you · 3 agent reports')
assert.equal(countsLine({ need: 0, filtered: 0, reports: 0 }), '')
assert.equal(countsLine(), '')
// only the filtered count is a "(show)"
assert.deepEqual(countsParts({ need: 2, filtered: 5, reports: 1 }).map((p) => !!p.show),
  [false, true, false])

assert.equal(runState({ running: true }), 'running')
assert.equal(runState({ running: false }), 'finished')
assert.equal(runState({ running: null }), '')
assert.equal(runState({}), '')

// open chat at the step, or just the chat
assert.equal(chatHref(500, 12123), '/c/500?step=12123')
assert.equal(chatHref(500), '/c/500')
assert.equal(chatHref(null, 5), null)

assert.equal(UNTRUSTED, "the agent's words: untrusted")

const proc = {
  id: 9, kind: 'unexpected_process', severity: 'warn', tier: 'alert',
  detail: { exe: '/usr/bin/node', unit: 'jarvis-guest.service', pid: 812, box_id: 'p-bg' },
  doing: { conversation: { id: 500 }, step: { id: 12123, tool: 'run_code' } },
}
assert.deepEqual(programOf(proc), { exe: '/usr/bin/node', name: 'node', unit: 'jarvis-guest.service' })
assert.equal(programOf({ detail: {} }), null)
assert.equal(lineTitle(proc), 'New program outside the box baseline')
assert.equal(lineTitle({ kind: 'write_flag', summary: 'write flag: x in a.py' }), 'write flag: x in a.py')

// the process alert: allow, open chat at the step, stop, kill, mute, acknowledge
const ids = (ev, ctx) => eventActions(ev, ctx).map((a) => a.id)
assert.deepEqual(ids(proc, { running: true, hasChat: true }),
  ['allow', 'chat', 'stop', 'kill', 'mute', 'ack'])
assert.equal(eventActions(proc, { hasChat: true }).find((a) => a.id === 'chat').label,
  'Open chat at step')
// a finished run has nobody to stop; no chat means no chat link and no stop
assert.deepEqual(ids(proc, { running: false, hasChat: true }), ['allow', 'chat', 'kill', 'mute', 'ack'])
assert.deepEqual(ids(proc, { hasChat: false }), ['allow', 'kill', 'mute', 'ack'])
// no pid (an old alert): nothing to kill
assert.ok(!ids({ ...proc, detail: { exe: '/usr/bin/node' } }, { hasChat: true }).includes('kill'))

// a write flag: diff, revert, chat, stop, mute, acknowledge
const wf = { id: 3, kind: 'write_flag', severity: 'warn', tier: 'alert',
  detail: { path: 'tests/world.test.mjs', trigger: 'assertion_removed' } }
assert.deepEqual(ids(wf, { running: true, hasChat: true }),
  ['board', 'revert', 'chat', 'stop', 'mute', 'ack'])
assert.equal(eventActions(wf, { hasChat: true }).find((a) => a.id === 'chat').label, 'Open chat')
// a refused leak wrote nothing: no revert
assert.ok(!ids({ ...wf, detail: { path: 'a', refused: true } }, {}).includes('revert'))

// an anomaly cut: un-cut, the evidence; critical kinds are never mutable
const cut = { id: 4, kind: 'egress_anomaly', severity: 'critical', tier: 'critical',
  detail: { host: 'evil.example' } }
assert.deepEqual(ids(cut, { hasChat: true, running: true }), ['uncut', 'board', 'chat', 'stop', 'ack'])
assert.equal(mutable(cut), false)
assert.equal(mutable(wf), true)

// an agent report is "Mark resolved", and any other kind can be inspected
const fault = { id: 5, kind: 'harness_fault', severity: 'info', tier: 'record', detail: {} }
assert.equal(eventActions(fault, {}).slice(-1)[0].label, 'Mark resolved')
assert.ok(ids({ id: 6, kind: 'memory_proposed', severity: 'warn', tier: 'alert' }, {}).includes('board'))
// Acknowledge is always the last button
for (const ev of [proc, wf, cut, fault]) assert.equal(ids(ev, { hasChat: true }).slice(-1)[0], 'ack')

// "(node, crashpad)" only when there is more than one thing to name
assert.equal(subjectsText({ n: 2, subjects: ['node', 'crashpad'] }), '(node, crashpad)')
assert.equal(subjectsText({ n: 1, subjects: ['node'] }), '')
assert.equal(subjectsText({ n: 2, subjects: ['node'] }), '(node)')
assert.equal(subjectsText({ n: 1, subjects: [] }), '')

// muting a whole card leaves the critical kinds alone
const run = { kinds: [{ kind: 'unexpected_process', tier: 'alert', severity: 'warn' },
  { kind: 'write_flag', tier: 'alert', severity: 'warn' },
  { kind: 'egress_anomaly', tier: 'critical', severity: 'critical' }] }
assert.deepEqual(kindsToMute(run), ['unexpected_process', 'write_flag'])
assert.match(muteAsk(['a', 'b']).title, /2 kinds/)
assert.match(muteAsk(['a']).title, /Mute a\?/)

// stopping a whole run names how many agents, and the plan if one drives them
const q = stopRunAsk({ agents: [1, 2, 3], plans: ['proj'] }, 'chat 500')
assert.match(q.title, /whole run: chat 500/)
assert.match(q.body, /3 agents running and the plan of proj/)
assert.match(stopRunAsk({ agents: [1], plans: [] }).body, /^1 agent running would/)

// the project panel shows only its own cards; the totals match the badge
const runs = [{ key: 'a', project: 'p', counts: { need: 2, reports: 1 } },
  { key: 'b', project: 'q', counts: { need: 1, reports: 0 } },
  { key: 'c', project: null, counts: { need: 0, reports: 2 } }]
assert.deepEqual(cardsFor(runs, 'p').map((r) => r.key), ['a'])
assert.equal(cardsFor(runs, null).length, 3)
assert.deepEqual(cardTotals(runs), { need: 3, reports: 3, runs: 3 })
assert.deepEqual(cardTotals(undefined), { need: 0, reports: 0, runs: 0 })

assert.equal(clock('2026-10-01 05:31:12'), '05:31')
assert.equal(clock('2026-10-01T05:31:12Z'), '05:31')
assert.equal(clock(''), '')

console.log('securityRuns ok')
