// node --test frontend/src/slash/__tests__/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  completion, filterOptions, findCommand, matchCommands, parseInput, transcriptMarkdown,
} from '../parse.js'

const CMDS = [
  { name: 'help', help: '' },
  { name: 'new', aliases: ['clear'], help: '' },
  { name: 'sessions', aliases: ['resume', 'history'], usage: '[chat]', args: () => [] },
  { name: 'models', aliases: ['model'], usage: '[id]', args: () => [] },
  { name: 'rename', usage: '[title]' },
  { name: 'orchestration', aliases: ['orchestrate'], args: () => [] },
]

test('parseInput: what is a command', () => {
  assert.equal(parseInput('hello'), null)
  assert.equal(parseInput('see /x'), null)
  assert.equal(parseInput('a/b'), null)
  assert.equal(parseInput('/usr/bin/env'), null)          // a path, not a command
  assert.deepEqual(parseInput('/'), { name: '', arg: '', spaced: false })
  assert.deepEqual(parseInput('/Mod'), { name: 'mod', arg: '', spaced: false })
  assert.deepEqual(parseInput('/model '), { name: 'model', arg: '', spaced: true })
  assert.deepEqual(parseInput('/rename  My chat '), { name: 'rename', arg: 'My chat', spaced: true })
  assert.deepEqual(parseInput('/project create two words'),
                   { name: 'project', arg: 'create two words', spaced: true })
})

test('parseInput: // escapes', () => {
  assert.deepEqual(parseInput('//etc is a dir'), { escaped: true, text: '/etc is a dir' })
  assert.deepEqual(parseInput('//'), { escaped: true, text: '/' })
})

test('findCommand: names and aliases, any case', () => {
  assert.equal(findCommand(CMDS, 'resume').name, 'sessions')
  assert.equal(findCommand(CMDS, 'MODEL').name, 'models')
  assert.equal(findCommand(CMDS, 'nope'), null)
})

test('matchCommands: prefix, then alias, then substring; each once', () => {
  assert.deepEqual(matchCommands(CMDS, '').map((r) => r.cmd.name),
                   CMDS.map((c) => c.name))
  assert.deepEqual(matchCommands(CMDS, 'mo').map((r) => r.cmd.name), ['models'])
  const h = matchCommands(CMDS, 'h')
  assert.deepEqual(h.map((r) => r.cmd.name), ['help', 'sessions'])
  assert.equal(h[1].via, 'history')
  assert.deepEqual(matchCommands(CMDS, 'cle').map((r) => [r.cmd.name, r.via]), [['new', 'clear']])
  assert.deepEqual(matchCommands(CMDS, 'chest').map((r) => r.cmd.name), ['orchestration'])
  assert.deepEqual(matchCommands(CMDS, 'zz'), [])
  // an exact name ranks first
  assert.equal(matchCommands(CMDS, 'new')[0].cmd.name, 'new')
})

test('filterOptions: prefix before substring, meta searched', () => {
  const o = [{ value: 'deepseek/flash', label: 'Flash' },
             { value: 'openai/gpt', label: 'GPT', meta: 'fast' },
             { value: 'x/flashy', label: 'Other' }]
  assert.equal(filterOptions(o, '').length, 3)
  assert.deepEqual(filterOptions(o, 'fla').map((x) => x.value), ['deepseek/flash', 'x/flashy'])
  assert.deepEqual(filterOptions(o, 'fast').map((x) => x.value), ['openai/gpt'])
  assert.deepEqual(filterOptions(o, 'zzz'), [])
})

test('completion: commands with an argument get a space', () => {
  assert.equal(completion({ kind: 'cmd', cmd: CMDS[0] }), '/help')
  assert.equal(completion({ kind: 'cmd', cmd: CMDS[4] }), '/rename ')
  assert.equal(completion({ kind: 'option', value: 'deepseek/flash' }, CMDS[3]),
               '/models deepseek/flash')
})

test('transcriptMarkdown', () => {
  const md = transcriptMarkdown('T', [
    { role: 'user', content: 'hi' },
    { role: 'error', content: 'x' },
    { role: 'assistant', content: 'yo', activity: [{ name: 'web_read', ok: false }] },
  ])
  assert.equal(md, '# T\n\n## You\n\nhi\n\n## Jav3\n\n- web_read (failed)\nyo\n')
})
