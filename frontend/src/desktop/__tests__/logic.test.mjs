// node frontend/src/desktop/__tests__/logic.test.mjs
import assert from 'node:assert/strict'
import {
  SANDBOX_WARNING, WATCH_LABEL, boxOptionLabel, candidateBoxes, displayWsUrl, eventTouches, needLine,
  panelMode, pickBox, startBlocked, startLabel, viewersText,
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
assert.equal(WATCH_LABEL, 'watching · view only')
assert.match(SANDBOX_WARNING, /sandbox, not your computer/)
assert.match(SANDBOX_WARNING, /don’t sign in/)
assert.match(SANDBOX_WARNING, /agent can read/)

console.log('desktop logic ok')
