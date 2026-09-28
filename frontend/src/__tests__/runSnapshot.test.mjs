// node frontend/src/__tests__/runSnapshot.test.mjs
import assert from 'node:assert/strict'
import { snapshotEvents } from '../runSnapshot.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }

const J = 'a'.repeat(32)
const row = (id, parent, extra = {}) => ({
  id, kind: 'leader', summary: `[leader] node ${id}`, rollup: null,
  parent_conversation_id: parent, job_id: J, ...extra })

t('the head is a root even though a chat launched it', () => {
  const ev = snapshotEvents(10, [row(10, 3, { kind: 'head', summary: '[research] Cats' })])
  assert.deepEqual(ev, [{ type: 'node_spawned', node_id: 10, parent_id: null,
                          kind: 'head', title: 'Cats', depth: 0 }])
})

t('depth follows parents inside the job; rollups mark nodes done', () => {
  const ev = snapshotEvents(10, [row(10, null), row(11, 10), row(12, 11, { rollup: 'r' })])
  const spawned = ev.filter((e) => e.type === 'node_spawned')
  assert.deepEqual(spawned.map((e) => [e.node_id, e.parent_id, e.depth]),
                   [[10, null, 0], [11, 10, 1], [12, 11, 2]])
  assert.deepEqual(ev.filter((e) => e.type === 'node_done'),
                   [{ type: 'node_done', node_id: 12, rollup: 'r' }])
  assert.ok(!ev.some((e) => e.type === 'job_final'))
})

t('a finished head ends the snapshot with job_final', () => {
  const ev = snapshotEvents(10, [row(10, null, { rollup: 'done' }), row(11, 10)])
  assert.deepEqual(ev.at(-1), { type: 'job_final', job_id: J, root_id: 10 })
})

t('a parent cycle terminates', () => {
  const ev = snapshotEvents(1, [row(1, 2), row(2, 1)])
  assert.equal(ev.length, 2)
})

t('titles without a tag pass through', () => {
  assert.equal(snapshotEvents(1, [row(1, null, { summary: 'plain' })])[0].title, 'plain')
  assert.equal(snapshotEvents(1, [row(1, null, { summary: null })])[0].title, '')
})

console.log(`${n} passed`)
