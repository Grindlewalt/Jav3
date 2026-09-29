// node --test frontend/src/__tests__/turnEvents.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import {
  activityMark, applyTurnEvent, atBottom, clipText, errorLine, exitNote, failTurn, finishTurn,
  fmtCost, fmtElapsed, fmtMs, foldParts, keyArg, makeTurnFolder, newTurn, patchStreaming,
  streamingIndex, toolLine, turnStats,
} from '../turnEvents.js'

const turn = () => newTurn('hi', 1000)[1]
const tool = (id, name = 'write_file', extra = {}) => ({ type: 'tool', id, name, args: { path: `${id}.txt` }, ...extra })
const result = (id, ok = true, r = 'ok') => ({ type: 'tool_result', id, name: 'write_file', ok, result: r })

test('a tool row is stamped when it starts and timed when it lands', () => {
  let m = applyTurnEvent(turn(), tool('a'), 2000)
  assert.equal(m.parts[0].t0, 2000)
  assert.equal(m.parts[0].done, false)
  m = applyTurnEvent(m, result('a'), 3250)
  assert.equal(m.parts[0].done, true)
  assert.equal(m.parts[0].ms, 1250)
})

test('a result pairs with its own call by id, not by order', () => {
  let m = applyTurnEvent(turn(), tool('a'), 0)
  m = applyTurnEvent(m, tool('b'), 0)
  m = applyTurnEvent(m, result('b', false, 'error: nope'), 10)
  assert.equal(m.parts[0].done, false)
  assert.equal(m.parts[1].ok, false)
})

test('a result for a call this view never saw still gets a row (joined mid-call)', () => {
  let m = applyTurnEvent(turn(), result('zz', true, 'exit 0'), 5)
  assert.equal(m.parts.length, 1)
  assert.equal(m.parts[0].done, true)
  assert.equal(m.parts[0].ok, true)
  // ...but not a second row for a call the persisted seed already holds
  const seeded = { ...turn(), parts: [{ kind: 'tool', name: 'write_file', args: {}, done: true, ok: true, result: 'exit 0' }] }
  assert.equal(applyTurnEvent(seeded, result('zz', true, 'exit 0'), 5).parts.length, 1)
  // a result with no id and no open call is dropped as before
  assert.equal(applyTurnEvent(turn(), { type: 'tool_result', name: 'x', ok: true, result: '' }, 1).parts.length, 0)
})

test('tokens extend the open text run; a tool starts a new one', () => {
  let m = applyTurnEvent(turn(), { type: 'token', text: 'he' })
  m = applyTurnEvent(m, { type: 'token', text: 'llo' })
  m = applyTurnEvent(m, tool('a'), 0)
  m = applyTurnEvent(m, { type: 'token', text: 'again' })
  assert.deepEqual(m.parts.map((p) => p.kind), ['text', 'tool', 'text'])
  assert.equal(m.parts[0].text, 'hello')
})

test('a re-announced job draws one tree', () => {
  const j = { type: 'job', root_id: 9, title: 't' }
  const m = applyTurnEvent(applyTurnEvent(turn(), j), j)
  assert.equal(m.parts.length, 1)
})

test('finishing keeps the rows, marks unreported calls interrupted, and times the turn', () => {
  let m = applyTurnEvent(turn(), tool('a'), 1100)
  m = applyTurnEvent(m, result('a'), 1200)
  m = applyTurnEvent(m, tool('b'), 1300)
  const done = finishTurn(m, 'all done', 3000)
  assert.equal(done.content, 'all done')
  assert.equal(done.streaming, undefined)
  assert.equal(done.ms, 2000)
  assert.equal(done.activity.length, 2)
  assert.equal(done.activity[0].ok, true)
  assert.equal(done.activity[1].interrupted, true)
  assert.equal(done.activity[1].ok, false)
})

test('events land on the streaming message, not on whatever is last', () => {
  const list = [{ role: 'user', content: 'x' }, turn(), { role: 'user', content: 'queued', midturn: 'queued' }]
  assert.equal(streamingIndex(list), 1)
  const next = patchStreaming(list, (x) => applyTurnEvent(x, { type: 'token', text: 'a' }))
  assert.equal(next[1].parts[0].text, 'a')
  assert.equal(next[2], list[2])
  // nothing streaming: nothing changes (a stale event for a settled turn)
  const settled = [{ role: 'assistant', content: 'done' }]
  assert.equal(patchStreaming(settled, () => { throw new Error('no') }), settled)
})

test('an error keeps the run and appends the error after it', () => {
  let m = applyTurnEvent(turn(), tool('a'), 1100)
  m = applyTurnEvent(m, result('a'), 1200)
  m = applyTurnEvent(m, tool('b'), 1300)
  m = applyTurnEvent(m, { type: 'token', text: 'so far' }, 1400)
  const out = failTurn([{ role: 'user', content: 'go' }, m], 'model went away', 2000)
  assert.deepEqual(out.map((x) => x.role), ['user', 'assistant', 'error'])
  assert.equal(out[1].activity.length, 2)
  assert.equal(out[1].content, 'so far')
  assert.equal(out[2].content, 'model went away')
  // no activity at all: just the error, in the placeholder's place
  const bare = failTurn([{ role: 'user', content: 'go' }, turn()], 'boom')
  assert.deepEqual(bare.map((x) => x.role), ['user', 'error'])
  // nothing streaming: appended
  assert.deepEqual(failTurn([{ role: 'user', content: 'go' }], 'late').map((x) => x.role), ['user', 'error'])
})

test('text tokens are batched, and anything else flushes them first', () => {
  let state = [{ role: 'user', content: 'go' }, turn()]
  const setMessages = (fn) => { state = fn(state) }
  const timers = []
  const f = makeTurnFolder(setMessages, {
    now: () => 5000,
    schedule: (fn) => { timers.push(fn); return timers.length },
    unschedule: () => {},
  })
  f.handle({ type: 'token', text: 'a' })
  f.handle({ type: 'token', text: 'b' })
  f.handle({ type: 'token', text: 'c' })
  assert.equal(timers.length, 1)                     // one timer for three tokens
  assert.equal(state[1].parts.length, 0)             // nothing applied yet
  timers[0]()
  assert.equal(state[1].parts[0].text, 'abc')
  // a tool call right after tokens: the tokens land first, in order
  f.handle({ type: 'token', text: 'd' })
  f.handle(tool('t1'))
  assert.deepEqual(state[1].parts.map((p) => p.kind), ['text', 'tool'])
  assert.equal(state[1].parts[0].text, 'abcd')
  f.handle({ type: 'final', content: 'fin' })
  assert.equal(state[1].content, 'fin')
  // cancel drops what is buffered
  f.handle({ type: 'token', text: 'zz' })
  f.cancel()
  assert.equal(state[1].content, 'fin')
})

test('turnStats counts calls, running and failed (an interrupted call is not a failure)', () => {
  const s = turnStats([
    { kind: 'text', text: 'x' },
    { kind: 'tool', done: true, ok: true },
    { kind: 'tool', done: true, ok: false },
    { kind: 'tool', done: true, ok: false, interrupted: true },
    { kind: 'tool', done: false, name: 'run_code' },
  ])
  assert.deepEqual({ ...s, current: s.current.name }, { tools: 4, running: 1, failed: 1, current: 'run_code' })
})

test('activityMark grows with rows, not with tokens', () => {
  const a = { ...turn(), parts: [{ kind: 'text', text: 'a' }] }
  const b = { ...turn(), parts: [{ kind: 'text', text: 'abcdef' }] }
  assert.equal(activityMark([{ role: 'user' }, a]), activityMark([{ role: 'user' }, b]))
  assert.ok(activityMark([{ role: 'user' }, { ...a, parts: [...a.parts, { kind: 'tool' }] }]) > activityMark([{ role: 'user' }, a]))
})

test('one-line summaries: title and the argument that matters', () => {
  assert.deepEqual(toolLine('write_file', { path: 'a/b.txt', content: 'zzz' }), { glyph: 'write', title: 'write', arg: 'a/b.txt' })
  assert.deepEqual(toolLine('run_code', { command: 'ls -l\nrm x' }), { glyph: 'run', title: 'run', arg: 'ls -l' })
  assert.equal(toolLine('web_read', { url: 'https://example.com/x?y=1', extract: 'p' }).arg, 'example.com (extracting)')
  assert.equal(toolLine('ask_user', { questions: [{ question: 'Which suffix?' }] }).arg, 'Which suffix?')
  // an unknown tool: its own name, and the first telling argument
  assert.deepEqual(toolLine('browser_click', { element: 'ref_3', text: 'Sign in' }), { glyph: 'tool', title: 'browser_click', arg: 'Sign in' })
  assert.equal(toolLine('mystery', {}).arg, '')
  assert.equal(keyArg({ n: 3, note: 'hello' }), 'hello')
  assert.equal(keyArg(null), '')
  assert.equal(keyArg({ query: 'x'.repeat(200) }).length, 81)
})

test('exit codes and error lines', () => {
  assert.equal(exitNote('exit 0\nfine'), 'exit 0')
  assert.equal(exitNote('exit -9'), 'exit -9')
  assert.equal(exitNote('hello'), '')
  assert.equal(errorLine('error: file not found\nat x'), 'file not found')
  assert.equal(errorLine(''), 'failed')
})

test('durations, the turn clock and cost', () => {
  assert.equal(fmtMs(120), '120ms')
  assert.equal(fmtMs(3), '3ms')
  assert.equal(fmtMs(1400), '1.4s')
  assert.equal(fmtMs(12400), '12s')
  assert.equal(fmtMs(65000), '1m 05s')
  assert.equal(fmtMs(undefined), '')
  assert.equal(fmtElapsed(0), '0s')
  assert.equal(fmtElapsed(45999), '45s')
  assert.equal(fmtElapsed(133000), '2m 13s')
  assert.equal(fmtElapsed(3840000), '1h 04m')
  assert.equal(fmtCost(0.0049), '<$0.01')
  assert.equal(fmtCost(0.0234), '$0.02')
  assert.equal(fmtCost(0), '$0.00')
  assert.equal(fmtCost(null), '')
})

test('long results are clipped and say how much is left out', () => {
  assert.deepEqual(clipText('short', 10), { text: 'short', hidden: 0 })
  const c = clipText('x'.repeat(50), 20)
  assert.equal(c.text.length, 20)
  assert.equal(c.hidden, 30)
  assert.equal(clipText(null).text, '')
})

const done = (id, extra = {}) => ({ kind: 'tool', id, done: true, ok: true, ...extra })

test('a long run folds its older finished rows and keeps the newest', () => {
  const parts = Array.from({ length: 10 }, (_, i) => done(`t${i}`))
  const out = foldParts(parts, { keep: 4 })
  assert.equal(out[0].kind, 'fold')
  assert.equal(out[0].parts.length, 6)
  assert.deepEqual(out.slice(1).map((p) => p.id), ['t6', 't7', 't8', 't9'])
  // a short run does not fold
  assert.equal(foldParts(parts.slice(0, 5), { keep: 4 }).length, 5)
})

test('errors, running calls, jobs and text never fold, and split a fold', () => {
  const parts = [
    done('a'), done('b'), done('c'), { kind: 'tool', id: 'bad', done: true, ok: false },
    done('d'), done('e'), done('f'), { kind: 'text', text: 'note' },
    done('g'), done('h'), done('i'), { kind: 'job', root_id: 1 },
    { kind: 'tool', id: 'live', done: false }, done('j'),
  ]
  const out = foldParts(parts, { keep: 1, min: 3 })
  assert.deepEqual(out.map((p) => p.kind), ['fold', 'tool', 'fold', 'text', 'fold', 'job', 'tool', 'tool'])
  assert.equal(out[1].id, 'bad')
  assert.deepEqual(out[0].parts.map((p) => p.id), ['a', 'b', 'c'])
  // folding is stable: more rows after it keep the same first row, so the same key
  const more = foldParts([...parts, done('k')], { keep: 1, min: 3 })
  assert.equal(more[0].key, out[0].key)
})

test('following the bottom', () => {
  assert.equal(atBottom(900, 100, 1000), true)
  assert.equal(atBottom(860, 100, 1000), true)     // within the slack
  assert.equal(atBottom(500, 100, 1000), false)
  assert.equal(atBottom(0, 100, 100), true)        // nothing to scroll
})
