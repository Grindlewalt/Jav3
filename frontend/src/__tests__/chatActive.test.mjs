// node frontend/src/__tests__/chatActive.test.mjs
import assert from 'node:assert/strict'
import { activeFrom } from '../chatActive.js'

// a plain chat streaming its own turn: only in /api/chat/running
let m = activeFrom([7], [])
assert.deepEqual(m.get(7), { running: true, agents: 0, needs: null })

// an orchestrator chat (root 1) running two agents, one finished; a second
// root (4) idle itself with a child waiting on the operator
m = activeFrom([1, 2, 3], [
  { id: 1, parent_id: null, status: 'running' },
  { id: 2, parent_id: 1, status: 'running' },
  { id: 3, parent_id: 2, status: 'running' },
  { id: 9, parent_id: 1, status: 'done' },
  { id: 4, parent_id: null, status: 'done' },
  { id: 5, parent_id: 4, status: 'needs_you', needs: 'approve a commit' },
])
assert.deepEqual(m.get(1), { running: true, agents: 2, needs: null })
assert.deepEqual(m.get(4), { running: false, agents: 0, needs: 'approve a commit' })
// child ids also come back as running loops; the sidebar renders only ids in
// its conversation list, so they are harmless here
assert.equal(m.get(2).running, true)
assert.equal(m.has(9), false)

// nothing live: empty
assert.equal(activeFrom([], [{ id: 1, parent_id: null, status: 'done' }]).size, 0)
assert.equal(activeFrom(undefined, undefined).size, 0)
console.log('chatActive ok')
