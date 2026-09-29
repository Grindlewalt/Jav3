// node frontend/src/__tests__/logsList.test.mjs
import assert from 'node:assert/strict'
import { mergeConvos } from '../logsList.js'

const c = (id, summary = `chat ${id}`) => ({ id, summary })

// the first load takes the server's list as it is
assert.deepEqual(mergeConvos(null, [c(3), c(2), c(1)]), { list: [c(3), c(2), c(1)], newCount: 0 })
assert.deepEqual(mergeConvos(undefined, undefined), { list: [], newCount: 0 })

// a new conversation is counted, not inserted: rows keep their place under the pointer
let m = mergeConvos([c(3), c(2), c(1)], [c(4), c(3), c(2), c(1)])
assert.deepEqual(m.list.map((x) => x.id), [3, 2, 1])
assert.equal(m.newCount, 1)

// a changed row is updated where it stands (the title arrives after the first message)
m = mergeConvos([c(3, 'new chat'), c(2)], [c(3, 'Tide table'), c(2)])
assert.equal(m.list[0].summary, 'Tide table')
assert.equal(m.newCount, 0)

// a deleted conversation goes; two new ones are two
m = mergeConvos([c(3), c(2), c(1)], [c(6), c(5), c(3), c(1)])
assert.deepEqual(m.list.map((x) => x.id), [3, 1])
assert.equal(m.newCount, 2)

// nothing changed: same rows, nothing waiting
m = mergeConvos([c(2), c(1)], [c(2), c(1)])
assert.deepEqual(m, { list: [c(2), c(1)], newCount: 0 })

console.log('logsList ok')
