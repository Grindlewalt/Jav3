// node frontend/src/desktop/__tests__/logic.test.mjs
import assert from 'node:assert/strict'
import {
  AGENT_ACTIVE_MS, SANDBOX_WARNING, agentActive, boxOptionLabel, candidateBoxes, canTake, displayWsUrl,
  eventTouches, heldSeconds, mmss, needLine, newViewerId, panelMode, pickBox, startBlocked, startLabel,
  statusLabel, stopTurns, takeLine, takesOver, viewOf, viewersText,
} from '../logic.js'

const rows = [
  { id: 'shared', kind: 'shared', runtime: 'kvm', projects: [] },
  { id: 'p-game', kind: 'project', runtime: 'kvm', projects: ['game'] },
  { id: 'p-web', kind: 'project', runtime: 'docker', projects: ['web'] },
  { id: 'p-both', kind: 'project', runtime: 'kvm', projects: ['both', 'game2'] },
  { id: 's-svc', kind: 'service', runtime: 'kvm', projects: [] },
]

// service and builder boxes are not offered; a Docker project box is, so it can say why not
assert.deepEqual(candidateBoxes(rows).map((b) => b.id), ['shared', 'p-game', 'p-web', 'p-both'])
assert.equal(boxOptionLabel(rows[2]), 'p-web (docker)')
assert.equal(boxOptionLabel(rows[1]), 'p-game')
assert.deepEqual(candidateBoxes(null), [])

// the operator's choice wins, then the project's own box, then one that serves it
assert.equal(pickBox(rows, 'game', null).id, 'p-game')
assert.equal(pickBox(rows, 'game', 'shared').id, 'shared')
assert.equal(pickBox(rows, 'game', 'p-gone').id, 'p-game')      // a stale choice falls back
assert.equal(pickBox(rows, 'game2', null).id, 'p-both')
assert.equal(pickBox(rows, 'web', null).id, 'p-web')            // docker: picked, and refused by name
assert.equal(pickBox(rows, 'nobody', null), null)
assert.equal(pickBox(rows, null, null), null)

const stopped = { supported: true, session: 'stopped', state: 'stopped', need_mb: 1424,
                  free_mb: 1034, fits: false }
assert.equal(needLine(stopped), 'needs 1424 MB, 1034 MB free')
assert.equal(startLabel(stopped), 'Start desktop · needs 1424 MB, 1034 MB free')
assert.equal(startBlocked(stopped), 'needs 1424 MB, 1034 MB free: stop another box first')
assert.equal(startBlocked({ ...stopped, fits: true, free_mb: 2000 }), null)

// a running box adds only the screen: no numbers, nothing blocking
const up = { supported: true, session: 'off', state: 'running', need_mb: 0, free_mb: 100, fits: true }
assert.equal(startLabel(up), 'Start desktop')
assert.equal(startBlocked(up), null)
assert.equal(startLabel(null), 'Start desktop')
assert.equal(startBlocked(null), 'checking…')

// refusals carry the server's own words
const docker = { supported: false, reason: 'a Docker box has no desktop: the desktop image is a KVM layer.' }
assert.match(startBlocked(docker), /Docker box has no desktop/)
assert.equal(startBlocked({ supported: true, session: 'unavailable', note: 'Restart it' }), 'Restart it')
assert.equal(startBlocked({ supported: true, session: 'missing', note: 'rebuild the image' }),
  'rebuild the image')

// what the body shows
assert.equal(panelMode(null, 'idle', null), 'loading')
assert.equal(panelMode(null, 'idle', 'boom'), 'error')
assert.equal(panelMode(docker, 'idle', null), 'unsupported')
assert.equal(panelMode(stopped, 'idle', null), 'idle')
assert.equal(panelMode(up, 'idle', null), 'idle')
const live = { ...up, session: 'running' }
assert.equal(panelMode(live, 'connecting', null), 'screen')
assert.equal(panelMode(live, 'watching', null), 'screen')
assert.equal(panelMode(live, 'lost', null), 'ended')
assert.equal(panelMode(live, 'closed', null), 'ended')

assert.equal(viewersText(1), '')
assert.equal(viewersText(3), '3 watching')

assert.equal(displayWsUrl({ protocol: 'http:', host: '10.0.0.82:8000' }, 'p-game'),
  'ws://10.0.0.82:8000/api/vm/boxes/p-game/display/ws')
assert.equal(displayWsUrl({ protocol: 'https:', host: 'jarvis.example' }, 'p-game'),
  'wss://jarvis.example/api/vm/boxes/p-game/display/ws')

assert.equal(eventTouches({ type: 'display', box_id: 'p-game' }, 'p-game'), true)
assert.equal(eventTouches({ type: 'display', box_id: 'p-x' }, 'p-game'), false)
assert.equal(eventTouches({ type: 'box_down', box: { id: 'p-game' } }, 'p-game'), true)
assert.equal(eventTouches({ type: 'box_event', box_id: 'p-game' }, 'p-game'), true)
assert.equal(eventTouches({ type: 'stream_open' }, 'p-game'), false)
assert.equal(eventTouches(null, 'p-game'), false)

// the words the page must say
assert.match(SANDBOX_WARNING, /sandbox, not your computer/)
assert.match(SANDBOX_WARNING, /don’t sign in/)
assert.match(SANDBOX_WARNING, /type anything private/)
assert.match(SANDBOX_WARNING, /agent can read/)

// the viewer id the server's regexp takes (8-64 of [A-Za-z0-9_-]), different each time
for (const r of [Math.random, () => 0, () => 0.999999]) {
  assert.match(newViewerId(r), /^[A-Za-z0-9_-]{8,64}$/)
}
assert.notEqual(newViewerId(), newViewerId())
assert.equal(displayWsUrl({ protocol: 'http:', host: 'h:8000' }, 'p-game', 'v-abc12345'),
  'ws://h:8000/api/vm/boxes/p-game/display/ws?viewer=v-abc12345')

assert.equal(mmss(41), '00:41')
assert.equal(mmss(125), '02:05')
assert.equal(mmss(3725), '1:02:05')
assert.equal(mmss(-3), '00:00')
assert.equal(mmss(undefined), '00:00')

// "agent driving" while its last action is recent, counted on from when the status arrived
assert.equal(agentActive(null, 1000), false)
assert.equal(agentActive({ active_age_s: null, turns: [] }, 1000), false)
assert.equal(agentActive({ active_age_s: 0, at: 1000 }, 1000 + AGENT_ACTIVE_MS - 1), true)
assert.equal(agentActive({ active_age_s: 0, at: 1000 }, 1000 + AGENT_ACTIVE_MS), false)
assert.equal(agentActive({ active_age_s: 25, at: 1000 }, 1000 + 6000), false)       // already 25 s old

// who holds it, as this window sees it
const me = 'v-me'
const mine = { holder: 'operator', viewer: me, by: 'grant', held_s: 41, at: 5000 }
const theirs = { ...mine, viewer: 'v-other' }
const agentHas = { holder: 'agent', viewer: null, by: null, held_s: 0, at: 5000 }
assert.equal(viewOf(mine, me, true), 'you')            // control beats an agent that just acted
assert.equal(viewOf(theirs, me, false), 'other')
assert.equal(viewOf(agentHas, me, true), 'agent')
assert.equal(viewOf(agentHas, me, false), 'watching')
assert.equal(viewOf(null, me, false), 'watching')
assert.equal(heldSeconds(mine, 5000 + 2500), 43)       // 41 s when it arrived, 2 s ago
assert.equal(heldSeconds(agentHas, 9000), 0)

assert.equal(statusLabel('you', 41), 'YOU have control: agent paused 00:41')
assert.equal(statusLabel('agent', 0), '● agent driving')
assert.equal(statusLabel('watching', 0), 'watching')
assert.equal(statusLabel('other', 0), 'another window has control')
assert.equal(takeLine('agent'), 'Agent is driving · click the screen to take over')
assert.match(takeLine('watching'), /click the screen to take over/i)
assert.match(takeLine('other'), /Another window/)

// a click takes control only from a connected screen that nobody holds
assert.equal(canTake('agent', 'watching'), true)
assert.equal(canTake('watching', 'watching'), true)
assert.equal(canTake('watching', 'connecting'), false)
assert.equal(canTake('you', 'watching'), false)
assert.equal(canTake('other', 'watching'), false)
assert.equal(takesOver('a'), true)
assert.equal(takesOver('Enter'), true)
assert.equal(takesOver('Tab'), false)
assert.equal(takesOver('Shift'), false)

// [Stop] has something to stop only while the agent is driving
const driving = { active_age_s: 1, turns: [12, 14], at: 0 }
assert.deepEqual(stopTurns('agent', true, driving), [12, 14])
assert.deepEqual(stopTurns('watching', false, driving), [])
assert.deepEqual(stopTurns('agent', true, { turns: [] }), [])
assert.deepEqual(stopTurns('you', true, driving), [])
assert.deepEqual(stopTurns('agent', true, null), [])

console.log('desktop logic ok')
