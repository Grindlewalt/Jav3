// node --test frontend/src/slash/__tests__/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  boxesLine, contextLine, filesLine, statusRows, toggleToolRows, usageLine,
} from '../status.js'

test('usageLine: tokens, cost, calls', () => {
  assert.equal(usageLine({ input_tokens: 41000, output_tokens: 1200, cost_usd: 0.004, calls: 4 }),
               '41,000 in · 1,200 out · $0.0040 · 4 calls')
  assert.equal(usageLine({ input_tokens: 5, output_tokens: 0, cost_usd: 0, calls: 1 }),
               '5 in · 0 out · $0.0000 · 1 call')
  assert.equal(usageLine(null), '')
})

test('contextLine: against the window when there is one', () => {
  assert.equal(contextLine({ used: 41000, window: 100000 }), '41,000 of 100,000 tokens (41%)')
  assert.equal(contextLine({ used: 300, window: null }), '300 tokens')
  assert.equal(contextLine(null), '')
  assert.equal(contextLine({ used: 0, window: 100 }), '')
})

test('filesLine: three, then "and n more"', () => {
  assert.equal(filesLine([]), '')
  assert.equal(filesLine([{ path: 'a.py' }, { path: 'b.py' }]), 'a.py, b.py')
  assert.equal(filesLine(['a', 'b', 'c', 'd', 'e'].map((path) => ({ path }))), 'a, b, c and 2 more')
})

test('boxesLine: running of all, with the RAM budget; the legacy shared VM', () => {
  const boxes = [
    { id: 'shared', kind: 'shared', state: 'running' },
    { id: 'p-demo', kind: 'project', state: 'running' },
    { id: 'p-old', kind: 'project', state: 'stopped' },
  ]
  assert.equal(boxesLine({ enabled: true, boxes, budget: { ram_mb_used: 1536, ram_mb_cap: 6144 } }),
               '2 of 3 running: shared, p-demo · RAM 1,536 / 6,144 MB')
  assert.equal(boxesLine({ enabled: true, boxes, budget: null }), '2 of 3 running: shared, p-demo')
  assert.equal(boxesLine({ enabled: true, boxes: [{ id: 'x', state: 'stopped' }] }),
               '0 of 1 running')
  assert.equal(boxesLine({ enabled: false, boxes: [boxes[0]] }), 'shared VM running')
  assert.equal(boxesLine({ legacy: true, enabled: false,
                           boxes: [{ id: 'shared', kind: 'shared', state: 'stopped' }] }),
               'shared VM stopped')
  assert.equal(boxesLine(null), '')
  assert.equal(boxesLine({ enabled: true }), '')
  const many = Array.from({ length: 5 }, (_, i) => ({ id: `b${i}`, state: 'running' }))
  assert.equal(boxesLine({ enabled: true, boxes: many }), '5 of 5 running: b0, b1, b2 and 2 more')
})

test('statusRows: the five that always answer, the rest only with something to say', () => {
  const bare = statusRows({ origin: 'https://j.example', user: 'grant', chat: null,
                            temporary: false, model: null, project: '' })
  assert.deepEqual(bare, [
    ['server', 'https://j.example'], ['signed in as', 'grant'],
    ['chat', 'new chat (not saved yet)'], ['model', '(none)'], ['project', 'none'],
  ])
  const full = statusRows({
    origin: 'https://j.example', user: 'grant', chat: { id: 9, title: 'config tweak' },
    temporary: false, model: { id: 'deepseek/deepseek-flash', label: 'Flash' },
    project: 'demo',
    info: { agent: 'coder', input_tokens: 41000, output_tokens: 1200, cost_usd: 0.004, calls: 4,
            context: { used: 41000, window: 100000 }, files: [{ path: 'app/config.py', writes: 1 }] },
    boxes: { enabled: true, boxes: [{ id: 'shared', state: 'running' }], budget: null },
  })
  assert.deepEqual(Object.fromEntries(full), {
    server: 'https://j.example', 'signed in as': 'grant', chat: '#9 config tweak',
    model: 'Flash (deepseek/deepseek-flash)', project: 'demo', agent: 'coder',
    usage: '41,000 in · 1,200 out · $0.0040 · 4 calls',
    context: '41,000 of 100,000 tokens (41%)', files: 'app/config.py',
    boxes: '1 of 1 running: shared',
  })
  assert.deepEqual(full.map((r) => r[0]),
                   ['server', 'signed in as', 'chat', 'model', 'project', 'agent', 'usage',
                    'context', 'files', 'boxes'])
  // a chat that is not saved and is temporary says so; a model without a label shows its id
  const temp = statusRows({ origin: 'o', user: 'u', chat: null, temporary: true,
                            model: { id: 'x/y', label: 'x/y' }, project: 'none' })
  assert.equal(temp[2][1], 'new temporary chat (nothing is saved)')
  assert.equal(temp[3][1], 'x/y')
})

// a tool row as ToolActivity.jsx draws it: a head that toggles, and a chevron once done
function fakeRow({ done = true, open = false } = {}) {
  const head = {
    clicks: 0,
    querySelector: (sel) => {
      if (!done) return null
      if (sel === '.chev') return {}
      if (sel === '.chev.open') return open ? {} : null
      return null
    },
    click() { this.clicks += 1; open = !open },
  }
  return head
}
const root = (heads) => ({ querySelectorAll: (sel) => (sel === '.tool-row-head' ? heads : []) })

test('toggleToolRows: opens the closed ones, then closes them all', () => {
  const a = fakeRow(), b = fakeRow({ open: true }), c = fakeRow(), running = fakeRow({ done: false })
  const r = root([a, b, c, running])
  assert.deepEqual(toggleToolRows(r), { opened: 2, closed: 0 })
  assert.deepEqual([a.clicks, b.clicks, c.clicks, running.clicks], [1, 0, 1, 0])
  // now all three are open: the next call closes them
  assert.deepEqual(toggleToolRows(r), { opened: 0, closed: 3 })
  assert.deepEqual([a.clicks, b.clicks, c.clicks], [2, 1, 2])
  assert.deepEqual(toggleToolRows(root([])), { opened: 0, closed: 0 })
  assert.deepEqual(toggleToolRows(root([fakeRow({ done: false })])), { opened: 0, closed: 0 })
})
