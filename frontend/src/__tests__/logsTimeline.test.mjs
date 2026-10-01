// node --test frontend/src/__tests__/logsTimeline.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { proseLabel, timelineHeader } from '../logsTimeline.js'

test('the header counts the agent notes only when there are some', () => {
  const items = [{ kind: 'message', role: 'user' }, { kind: 'narration', text: 'a' },
    { kind: 'tool' }, { kind: 'narration', text: 'b' }, { kind: 'message', role: 'assistant' }]
  assert.equal(timelineHeader(items), '5 items · 2 agent notes · in order')
  assert.equal(timelineHeader(items.slice(0, 2)), '2 items · 1 agent note · in order')
  assert.equal(timelineHeader([{ kind: 'message', role: 'user' }]), '1 item · in order')
  assert.equal(timelineHeader(undefined), '0 items · in order')
})

test('narration is labelled as the agent\'s own text, a message by its role', () => {
  assert.equal(proseLabel({ kind: 'narration' }), "agent's own text")
  assert.equal(proseLabel({ kind: 'message', role: 'assistant' }), 'assistant')
})
