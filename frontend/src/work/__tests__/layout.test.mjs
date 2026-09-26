// node frontend/src/work/__tests__/layout.test.mjs
import assert from 'node:assert/strict'
import {
  addCard, close, closeCard, classicBoard, fromSaved, geometry, leaves, makeLeaf,
  neighbor, patchCardState, reconcile, resize, split, switchCard, toSaved, treeFromLegacy,
} from '../layout.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }
const types = (root) => leaves(root).map((l) => l.type).join(',')
const sum = (a) => a.reduce((x, y) => x + y, 0)
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`)

t('default board: chat | board | git over network', () => {
  const b = classicBoard()
  assert.equal(types(b.root), 'chat,board,git,network')
  const g = geometry(b.root)
  near(g.leaves.p1.w, 0.36); near(g.leaves.p3.y, 0); near(g.leaves.p4.y, 0.54)
  assert.equal(g.dividers.length, 3)
  assert.equal(b.focus, 'p1')
})

t('split a lone leaf, then same-direction split inserts a sibling', () => {
  const a = makeLeaf('chat', 'a')
  let r = split(null, 'x', a)
  assert.equal(r, a)
  r = split(r, 'a', makeLeaf('git', 'b'), 'row')
  assert.equal(r.dir, 'row'); assert.deepEqual(r.sizes, [0.5, 0.5])
  r = split(r, 'a', makeLeaf('todos', 'c'), 'row')
  assert.equal(types(r), 'chat,todos,git')
  assert.deepEqual(r.sizes, [0.25, 0.25, 0.5])
  r = split(r, 'b', makeLeaf('run', 'd'), 'col', true)
  assert.equal(types(r), 'chat,todos,run,git')
  assert.equal(r.children[2].dir, 'col')
  near(sum(r.sizes), 1)
})

t('close gives space to the neighbour and collapses', () => {
  let r = split(makeLeaf('chat', 'a'), 'a', makeLeaf('git', 'b'), 'row')
  r = split(r, 'b', makeLeaf('run', 'c'), 'col')
  r = close(r, 'c')
  assert.equal(types(r), 'chat,git'); assert.equal(r.dir, 'row')
  r = close(r, 'a'); assert.equal(r.id, 'b')
  assert.equal(close(r, 'b'), null)
})

t('close flattens a same-direction split left behind', () => {
  let r = split(makeLeaf('x', 'a'), 'a', makeLeaf('x', 'b'), 'row')
  r = split(r, 'b', makeLeaf('x', 'c'), 'col')
  r = split(r, 'c', makeLeaf('x', 'd'), 'row')
  r = close(r, 'b')
  assert.equal(r.dir, 'row'); assert.equal(r.children.length, 3)
  near(sum(r.sizes), 1); near(r.sizes[1], 0.25)
})

t('resize clamps to min and keeps the pair total', () => {
  const r0 = split(makeLeaf('x', 'a'), 'a', makeLeaf('x', 'b'), 'row')
  let r = resize(r0, r0.id, 0, 0.1)
  near(r.sizes[0], 0.6); near(r.sizes[1], 0.4)
  r = resize(r0, r0.id, 0, 5, 0.1); near(r.sizes[0], 0.9)
  r = resize(r0, r0.id, 0, -5, 0.1); near(r.sizes[0], 0.1)
  assert.equal(resize(r0, r0.id, 3, 0.1), r0)
  assert.deepEqual(r0.sizes, [0.5, 0.5], 'input untouched')
})

t('neighbor', () => {
  const b = classicBoard()
  assert.equal(neighbor(b.root, 'p1', 'right'), 'p2')
  assert.equal(neighbor(b.root, 'p3', 'down'), 'p4')
  assert.equal(neighbor(b.root, 'p1', 'left'), null)
})

t('board ops: add, switch, close, state', () => {
  let b = classicBoard()
  b = addCard(b, 'terminal', 'p3', 'col')
  const term = b.focus
  assert.equal(types(b.root), 'chat,board,git,terminal,network')
  assert.ok(b.panels.some((p) => p.id === term && p.type === 'terminal'))
  b = patchCardState(b, term, { cwd: '/x' })
  assert.deepEqual(b.panels.find((p) => p.id === term).state, { cwd: '/x' })
  b = switchCard(b, term, 'todos')
  assert.ok(!b.panels.some((p) => p.id === term), 'old card gone')
  assert.deepEqual(b.panels.find((p) => p.id === b.focus).state, {})
  b = closeCard(b, b.focus)
  assert.equal(types(b.root), 'chat,board,git,network')
  assert.equal(b.panels.length, 4)
  for (const id of ['p1', 'p2', 'p3', 'p4']) b = closeCard(b, id)
  assert.equal(b.root, null); assert.equal(b.focus, null); assert.equal(b.panels.length, 0)
})

t('legacy fitted default converts to three columns', () => {
  const old = [
    { id: 'p1', type: 'chat', x: 16, y: 16, w: 470, h: 700, z: 1, state: { a: 1 } },
    { id: 'p2', type: 'board', x: 502, y: 16, w: 400, h: 700, z: 2 },
    { id: 'p3', type: 'git', x: 918, y: 16, w: 450, h: 372, z: 3 },
    { id: 'p4', type: 'network', x: 918, y: 404, w: 450, h: 312, z: 4 },
  ]
  const { board, converted } = fromSaved({ panels: old })
  assert.ok(converted)
  assert.equal(types(board.root), 'chat,board,git,network')
  assert.equal(board.root.dir, 'row'); assert.equal(board.root.children.length, 3)
  assert.equal(board.root.children[2].dir, 'col')
  assert.deepEqual(board.panels.find((p) => p.id === 'p1').state, { a: 1 }, 'card state kept')
  assert.equal(board.focus, 'p4', 'top z is focused')
})

t('legacy two stacked columns', () => {
  const r = treeFromLegacy([
    { id: 'a', type: 'chat', x: 0, y: 0, w: 500, h: 400 },
    { id: 'b', type: 'git', x: 0, y: 420, w: 500, h: 200 },
    { id: 'c', type: 'run', x: 520, y: 0, w: 300, h: 600 },
  ])
  assert.equal(types(r), 'chat,git,run')
  near(r.sizes[0], 500 / 800)
  near(r.children[0].sizes[0], 400 / 600)
})

t('Work: legacy chat cards dropped; empty/unarranged load empty', () => {
  const { board } = fromSaved({ panels: [
    { id: 'a', type: 'chat', x: 0, y: 0, w: 400, h: 400 },
    { id: 'b', type: 'git', x: 420, y: 0, w: 400, h: 400 },
  ] }, undefined, { legacyDrop: ['chat'] })
  assert.equal(types(board.root), 'git'); assert.equal(board.panels.length, 1)
  assert.equal(fromSaved(null).board.root, null)
  assert.equal(fromSaved({ panels: [{ id: 'a', type: 'chat', x: 0, y: 0, w: 1, h: 1 }] },
                         undefined, { legacyDrop: ['chat'] }).board.root, null)
})

t('legacy unfitted default and empty load the given fresh board', () => {
  const legacy = [
    ['p1', 'chat', 16, 16, 460, 560], ['p2', 'board', 492, 16, 400, 560],
    ['p3', 'git', 908, 16, 540, 300], ['p4', 'network', 908, 332, 540, 244],
  ].map(([id, type, x, y, w, h]) => ({ id, type, x, y, w, h }))
  const o = { fresh: classicBoard }
  assert.equal(types(fromSaved({ panels: legacy }, undefined, o).board.root), 'chat,board,git,network')
  assert.equal(types(fromSaved(null, undefined, o).board.root), 'chat,board,git,network')
})

t('unknown types are dropped', () => {
  const { board } = fromSaved({ panels: [
    { id: 'a', type: 'chat', x: 0, y: 0, w: 1, h: 1 }, { id: 'b', type: 'staging', x: 9, y: 0, w: 1, h: 1 },
  ] }, (t) => t !== 'staging')
  assert.equal(types(board.root), 'chat')
})

t('v2 round trip keeps tree, focus, maximized and state; x/y/w/h filled in', () => {
  let b = classicBoard()
  b = patchCardState(b, 'p2', { tab: 'runs' })
  b = { ...b, focus: 'p3', maximized: 'p2' }
  const saved = JSON.parse(JSON.stringify(toSaved(b)))
  assert.equal(saved.v, 2)
  const p4 = saved.panels.find((p) => p.id === 'p4')
  assert.ok(p4.x > 0 && p4.y > 0 && p4.w > 0 && p4.h > 0 && p4.z === 4)
  const { board, converted } = fromSaved(saved)
  assert.ok(!converted)
  assert.equal(types(board.root), types(b.root))
  const ga = geometry(board.root).leaves, gb = geometry(b.root).leaves
  for (const id of Object.keys(gb)) near(ga[id].w, gb[id].w)
  assert.equal(board.focus, 'p3'); assert.equal(board.maximized, 'p2')
  assert.deepEqual(board.panels.find((p) => p.id === 'p2').state, { tab: 'runs' })
})

t('v2 reconciles panels the agent added or removed server-side', () => {
  const saved = toSaved(classicBoard())
  saved.panels = saved.panels.filter((p) => p.id !== 'p2')          // workspace_panel close
  saved.panels.push({ id: 'p5', type: 'terminal', x: 2000, y: 16, w: 560, h: 360, state: {} })
  const { board } = fromSaved(saved)
  assert.equal(types(board.root), 'chat,terminal,git,network')
  const r = reconcile(null, [{ id: 'a', type: 'chat' }, { id: 'b', type: 'git' }], null)
  assert.equal(types(r), 'chat,git')
})

t('v2 with every card closed stays empty', () => {
  const { board } = fromSaved({ v: 2, panels: [], tree: null, focus: null })
  assert.equal(board.root, null)
})

console.log(`${n} passed`)
