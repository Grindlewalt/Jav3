// node frontend/src/boxes/__tests__/glance.test.mjs
import assert from 'node:assert/strict'
import {
  activityWord, doingNow, eventWord, idlePolicy, idleTimer, leftoverSummary, localTime, mins,
} from '../glance.js'

assert.deepEqual([0, 45, 60, 599, 3600, 3900].map(mins), ['0s', '45s', '1m', '9m', '1h00m', '1h05m'])

const proj = { state: 'running', idle_s: 240, stop_action: 'stop', stop_after_s: 600, stops_in_s: 360 }
assert.equal(idleTimer(proj).text, 'idle 4m (stops at 10m)')
assert.equal(idleTimer({ ...proj, stops_in_s: 30 }).tone, 'pending')
const shared = { state: 'running', idle_s: 240, stop_action: 'scrub', stop_after_s: 900, stops_in_s: 660 }
assert.equal(idleTimer(shared).text, 'scrub after 11m')
assert.equal(idleTimer({ ...shared, stops_in_s: undefined }).text, 'scrub after 11m')
assert.equal(idleTimer({ ...proj, idle_s: null }), null)            // a turn is running
assert.equal(idleTimer({ ...proj, state: 'stopped' }), null)
assert.equal(idleTimer({ ...proj, stop_action: null, stop_after_s: null }), null)

assert.equal(idlePolicy({ project_stop_s: 600, shared_scrub_s: 900 }, true),
  'project boxes stop after 10m idle · the shared box is scrubbed after 15m idle')
assert.equal(idlePolicy({ project_stop_s: 600, shared_scrub_s: null }, false),
  'the shared box is never scrubbed')
assert.equal(idlePolicy(null, true), '')

const d = doingNow({ now: [{ op_id: 'chat:42', conversation_id: 42, title: 'fix it', project: 'a',
  tool: { name: 'shell', detail: 'npm test', since: 1 } }, { op_id: 'x', conversation_id: null }] })
assert.equal(d.length, 2)
assert.deepEqual([d[0].head, d[0].tool, d[0].detail, d[1].head], ['#42', 'shell', 'npm test', 'a turn'])
assert.deepEqual(doingNow({}), [])

assert.equal(activityWord({ state: 'running', activity: 'busy' }), 'busy')
assert.equal(activityWord({ state: 'running', activity: 'idle' }), 'idle')
assert.equal(activityWord({ state: 'stopped', activity: 'failed' }), 'failed')
assert.equal(activityWord({ state: 'stopped' }), 'stopped')
assert.equal(eventWord({ event: 'idle_stopped' }), 'idle stopped')

assert.match(localTime('2026-09-28 14:03:00'), /^09-2[89] \d\d:03$/)
assert.match(localTime(Date.parse('2026-09-28T14:03:00Z') / 1000), /^09-2[89] \d\d:03$/)
assert.equal(localTime(null), '')
assert.equal(localTime('nonsense'), '')

assert.equal(leftoverSummary([{ type: 'container' }, { type: 'container' }, { type: 'box_dir' }]),
  '2 containers, 1 box directory')

console.log('glance ok')
