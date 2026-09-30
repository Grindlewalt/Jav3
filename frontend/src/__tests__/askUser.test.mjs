// node frontend/src/__tests__/askUser.test.mjs
import assert from 'node:assert/strict'
import {
  answerBody, foldAsks, initAsk, initialMode, keyToAction, MODE_LABEL, nextMode, PERMISSION_MODES,
  reduceAsk,
} from '../askUser.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }
const QS = [{ question: 'db?', options: ['SQLite', 'Postgres'] },
            { question: 'extras?', options: ['auth', 'admin', 'api'], multi_select: true }]
const run = (keys) => {
  let s = initAsk()
  for (const k of keys) {
    const a = k.startsWith('text:') ? { type: 'text', value: k.slice(5) } : keyToAction(k, s, QS)
    if (a === 'skip') return 'skipped'
    if (a && typeof a === 'object') s = reduceAsk(s, QS, a)
    if (s.done) return s.done
  }
  return s
}

t('digit picks, enter confirms and moves on, multi toggles', () => {
  assert.deepEqual(run(['2', 'Enter', '1', '3', ' ', 'Enter']), [
    { selected: ['Postgres'], text: null }, { selected: ['auth'], text: null }])
})
t('enter on an empty question takes the cursor row', () => {
  assert.deepEqual(run(['ArrowDown', 'Enter', 'ArrowDown', 'ArrowDown', 'Enter']), [
    { selected: ['Postgres'], text: null }, { selected: ['api'], text: null }])
})
t('typing replaces a single pick and selects the free-text option', () => {
  const s = run(['1', 'text:MySQL'])
  assert.deepEqual(s.sel, [])
  assert.equal(s.cur, 2)
  assert.deepEqual(run(['1', 'text:MySQL', 'Enter', '2', 'text:docs', 'Enter']), [
    { selected: [], text: 'MySQL' }, { selected: ['admin'], text: 'docs' }])
})
t('enter on an empty free-text row does nothing; esc skips', () => {
  const s = run(['ArrowDown', 'ArrowDown', 'Enter'])
  assert.equal(s.i, 0)
  assert.equal(s.done, null)
  assert.equal(run(['1', 'Escape']), 'skipped')
  assert.equal(keyToAction('x', initAsk(), QS), 'focus-text')
  assert.equal(keyToAction('3', initAsk(), QS), 'focus-text')   // only 2 options
})
t('answer body, event folding, mode cycle', () => {
  assert.deepEqual(answerBody('a', null), { id: 'a', skipped: true })
  assert.deepEqual(answerBody('a', [1]), { id: 'a', answers: [1] })
  let asks = foldAsks([], { type: 'ask_user', id: 'a' })
  asks = foldAsks(asks, { type: 'ask_user', id: 'a' })
  assert.equal(asks.length, 1)
  assert.deepEqual(foldAsks(asks, { type: 'ask_done', id: 'a' }), [])
  assert.deepEqual(['yolo', 'auto', 'ask'].map(nextMode), ['auto', 'ask', 'yolo'])
})
t('mode labels say what each mode does; a new chat starts where the last pick was', () => {
  for (const m of PERMISSION_MODES) {
    assert.ok(MODE_LABEL[m].startsWith(`${m}:`), m)      // the mode stays the first word
    assert.ok(MODE_LABEL[m].length > m.length + 10, m)   // and something follows it
  }
  assert.equal(initialMode('ask'), 'ask')
  assert.equal(initialMode('bogus'), 'yolo')
  assert.equal(initialMode(null), 'yolo')
})
console.log(`${n} passed`)
