// node --test frontend/src/slash/__tests__/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { TERMINAL_ONLY, terminalAnswer, terminalCommandNames } from '../terminal.js'
import { findCommand, matchCommands, parseInput } from '../parse.js'

const here = (p) => new URL(p, import.meta.url)
const terminalSource = readFileSync(here('../../../../clients/jav3cli/jav3'), 'utf8')
const commandsSource = readFileSync(here('../commands.js'), 'utf8')

// The web registry read from its source: commands.js imports browser modules
// (a bundler-style './tab'), so node cannot import it. Every command is a
// `name: '…'` line at the array's own indent, with an optional aliases list.
function webCommands() {
  const out = []
  const re = /^ {4}name: '([a-z][a-z0-9-]*)'(?:, aliases: \[([^\]]*)\])?/gm
  for (const m of commandsSource.matchAll(re)) {
    out.push({ name: m[1], aliases: m[2] ? [...m[2].matchAll(/'([^']+)'/g)].map((x) => x[1]) : [] })
  }
  return out
}

test('terminalAnswer: names, aliases, any case; nothing for a web command', () => {
  assert.match(terminalAnswer('themes'), /terminal client/)
  assert.equal(terminalAnswer('theme'), terminalAnswer('themes'))
  assert.equal(terminalAnswer('QUIT'), terminalAnswer('exit'))
  assert.equal(terminalAnswer('stop-project'), terminalAnswer('stop-a'))
  assert.equal(terminalAnswer('status'), null)
  assert.equal(terminalAnswer('nope'), null)
  assert.equal(terminalAnswer(''), null)
  for (const line of Object.values(TERMINAL_ONLY)) {
    assert.ok(line.length > 20 && line.length < 200, line)     // one helpful line, not a lecture
  }
})

test('terminalCommandNames reads the terminal client’s table', () => {
  const names = terminalCommandNames(terminalSource)
  assert.ok(names.length > 30, `only ${names.length} names`)
  for (const n of ['help', 'status', 'details', 'detach', 'editor', 'web', 'login', 'logout',
                   'themes', 'theme', 'quit', 'stop-all', 'stop-project', 'tools']) {
    assert.ok(names.includes(n), `missing ${n}`)
  }
})

test('the web registry is read as the source has it', () => {
  const web = webCommands()
  const byName = Object.fromEntries(web.map((c) => [c.name, c]))
  assert.ok(web.length >= 28, `only ${web.length} commands`)
  assert.deepEqual(byName.details.aliases, ['tools'])
  assert.deepEqual(byName.new.aliases, ['clear'])
  assert.deepEqual(byName.sessions.aliases, ['resume', 'history'])
})

test('every command the terminal client has is answered here, and themes are not in the app', () => {
  const web = webCommands()
  const known = new Set(web.flatMap((c) => [c.name, ...c.aliases]))
  const missing = terminalCommandNames(terminalSource)
    .filter((n) => !known.has(n) && !terminalAnswer(n))
  assert.deepEqual(missing, [],
    `terminal commands with no web command, alias or line in terminal.js: ${missing.join(', ')}`)
  // the operator does not want themes in the web app: answered, never a command
  assert.ok(!known.has('themes') && !known.has('theme'))
  assert.ok(terminalAnswer('themes'))
  // and nothing is both a command and a line (the line would never be seen)
  for (const n of Object.keys(TERMINAL_ONLY)) assert.ok(!known.has(n), `${n} is both`)
})

test('the new commands are found, complete and rank like the terminal’s', () => {
  const cmds = webCommands().map((c) => ({ ...c, help: '' }))
  for (const n of ['status', 'details', 'detach', 'editor', 'web', 'login', 'logout', 'vms']) {
    assert.ok(findCommand(cmds, n), n)
  }
  assert.equal(findCommand(cmds, 'tools').name, 'details')
  assert.equal(matchCommands(cmds, 'status')[0].cmd.name, 'status')
  assert.equal(matchCommands(cmds, 'logo')[0].cmd.name, 'logout')     // login does not start "logo"
  assert.deepEqual(matchCommands(cmds, 'theme'), [])                  // no row: Enter says why
  assert.deepEqual(parseInput('/logout yes'), { name: 'logout', arg: 'yes', spaced: true })
  assert.deepEqual(parseInput('/details'), { name: 'details', arg: '', spaced: false })
})
