// node --test frontend/src/__tests__/resume.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { RESUME_TEXT, isFailedRow, resumable, resumeUrl } from '../resume.js'
import { failTurn, newTurn } from '../turnEvents.js'

const user = (content) => ({ role: 'user', content })
const reply = (content, extra = {}) => ({ role: 'assistant', content, ...extra })

test('both saved forms of a dead turn are recognised, a normal reply is not', () => {
  assert.equal(isFailedRow(reply('(turn failed: guest closed the connection mid-turn)')), true)
  assert.equal(isFailedRow(reply('(guest loop error: ModelError: upstream 502)')), true)
  assert.equal(isFailedRow(reply('All three pass. The turn failed tests are fixed.')), false)
  assert.equal(isFailedRow(reply('[Request interrupted by operator]')), false)
  assert.equal(isFailedRow(user('(turn failed: pasted by hand)')), false)
  assert.equal(isFailedRow(reply('(turn failed: x)', { streaming: true })), false)
  assert.equal(isFailedRow(null), false)
})

test('Resume is offered only when the last message is the failed turn', () => {
  const dead = [user('build it'), reply('(turn failed: guest went away)')]
  assert.equal(resumable(dead), true)
  assert.equal(resumable([...dead, user('what is 2+2'), reply('4')]), false)
  assert.equal(resumable([...dead, user('continue')]), false)
  assert.equal(resumable([...dead, { role: 'error', content: 'a turn is still running' }]), false)
  assert.equal(resumable([user('hi'), reply('hello')]), false)
  assert.equal(resumable([]), false)
  assert.equal(resumable(undefined), false)
})

test('a stream that just ended in an error offers Resume, a refusal does not', () => {
  const live = failTurn(newTurn('go', 1000), 'guest closed the connection mid-turn', 2000)
  assert.equal(live[live.length - 1].failed, true)
  assert.equal(resumable(live), true)
  assert.equal(resumable([user('go'), { role: 'error', content: 'no such conversation' }]), false)
})

test('the resume call goes to the chat endpoint with the fixed message', () => {
  assert.equal(resumeUrl(500), '/api/chat/500/resume')
  assert.equal(RESUME_TEXT, 'Continue from where the previous turn stopped.')
})
