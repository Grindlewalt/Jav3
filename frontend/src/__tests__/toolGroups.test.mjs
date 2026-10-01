// node frontend/src/__tests__/toolGroups.test.mjs
import assert from 'node:assert/strict'
import { groupTools, heading, oneLine, rowMatches } from '../toolGroups.js'

const sections = [
  { name: 'files', about: 'project files', merged: '' },
  { name: 'browser', about: 'operate web pages', merged: 'Operate web pages in tabs.' },
  { name: 'plans', about: 'the running plan', merged: 'The running plan.' },
  { name: 'system', about: 'your own manual', merged: 'Your manual.' },
]
const row = (name, o = {}) => ({
  name, description: `${name} does a thing. More words.`, when_to_use: '', offered: true,
  section: 'files', action: null, merged_into: null, core: false, internal: false,
  gating: [], ...o,
})
const rows = [
  row('read_file', { core: true, gating: ['in-guest', 'needs project'] }),
  row('browser_read_page', {
    section: 'browser', action: 'read', merged_into: 'browser', offered: false,
    gating: ['needs extension'],
  }),
  row('browser_click', {
    section: 'browser', action: 'click', merged_into: 'browser', offered: false,
    gating: ['needs extension'],
  }),
  row('browser_list_tabs', {
    section: 'browser', action: 'list_tabs', merged_into: 'browser', offered: false,
    gating: ['needs extension', 'needs project'],
  }),
  row('plan_status', {
    section: 'plans', action: 'status', merged_into: 'plans', internal: true,
    gating: ['plan items only'],
  }),
  row('plan_report', {
    section: 'plans', action: 'report', merged_into: 'plans', internal: true,
    gating: ['plan items only'],
  }),
  row('inbox_fetch', { section: 'system', internal: true, gating: ['harness only'] }),
  row('ask_user', { section: 'system', core: true }),
]

// the default view: no internal tools, one card per tool the model sees
let g = groupTools(rows, sections)
assert.deepEqual(g.sections.map((s) => s.name), ['files', 'browser', 'system'])
assert.equal(heading(g), '3 tools · 5 actions')            // read_file, browser(3), ask_user
assert.equal(g.internalTools, 2)                            // plans, inbox_fetch
assert.equal(g.internalActions, 3)
const browser = g.sections[1].tools[0]
assert.equal(browser.name, 'browser')
assert.equal(browser.merged, true)
assert.equal(browser.about, 'Operate web pages in tabs.')
assert.deepEqual(browser.actions.map((a) => a.row.action), ['read', 'click', 'list_tabs'])
assert.deepEqual(browser.gating, ['needs extension'])        // shared by every action
assert.deepEqual(browser.actions.map((a) => a.gating), [[], [], ['needs project']])
assert.equal(browser.offered, 0)
assert.equal(browser.reason, '')                            // these rows give no reason
const readFile = g.sections[0].tools[0]
assert.equal(readFile.merged, false)
assert.equal(readFile.about, 'read_file does a thing.')    // a standalone tool: its own line
assert.deepEqual(readFile.gating, ['in-guest', 'needs project'])
assert.equal(readFile.core, true)
assert.equal(g.sections[2].tools.length, 1)                  // inbox_fetch is hidden

// one reason shared by every action is said once, on the tool
g = groupTools(rows.map((r) => (r.merged_into === 'browser' ? { ...r, reason: 'no extension' } : r)), sections)
assert.equal(g.sections[1].tools[0].reason, 'no extension')
g = groupTools(rows.map((r) => (r.action === 'click' ? { ...r, reason: 'no extension' } : r)), sections)
assert.equal(g.sections[1].tools[0].reason, '')              // they differ: each says its own

// show internal: plans comes back as one merged tool with two actions
g = groupTools(rows, sections, { internal: true })
assert.equal(heading(g), '5 tools · 8 actions')
assert.deepEqual(g.sections.map((s) => s.name), ['files', 'browser', 'plans', 'system'])
assert.equal(g.sections[2].tools[0].actions.length, 2)
assert.deepEqual(g.sections[2].tools[0].gating, ['plan items only'])
assert.equal(g.internalTools, 0)

// search: finds an action by its old name, by "tool action", by words in its text
g = groupTools(rows, sections, { query: 'browser_click' })
assert.equal(g.shownActions, 1)
assert.equal(g.sections[0].tools[0].name, 'browser')
assert.equal(g.sections[0].tools[0].actions[0].row.name, 'browser_click')
assert.equal(heading(g), '3 tools · 5 actions')              // the heading is the catalogue's
assert.equal(groupTools(rows, sections, { query: 'browser click' }).shownActions, 1)
assert.equal(groupTools(rows, sections, { query: 'browser' }).shownActions, 3)
assert.equal(groupTools(rows, sections, { query: 'needs extension' }).shownActions, 3)
assert.equal(groupTools(rows, sections, { query: 'ask thing' }).shownActions, 1)
assert.equal(groupTools(rows, sections, { query: 'nothing like it' }).sections.length, 0)
// an internal tool is not found until the toggle is on
assert.equal(groupTools(rows, sections, { query: 'plan_status' }).shownActions, 0)
assert.equal(groupTools(rows, sections, { query: 'plan_status', internal: true }).shownActions, 1)
assert.equal(rowMatches(rows[0], ''), true)

// empty and missing data
assert.equal(heading(groupTools([], [])), '0 tools · 0 actions')
assert.equal(heading(groupTools(null)), '0 tools · 0 actions')
assert.equal(heading({ tools: 1, actions: 1 }), '1 tool · 1 action')
// a section the API did not describe still shows, and a row with none is "other"
g = groupTools([row('x', { section: 'zzz' }), row('y', { section: undefined })], [])
assert.deepEqual(g.sections.map((s) => s.name), ['zzz', 'other'])

// one line of a description
assert.equal(oneLine('Search the web. Pages come back as text.'), 'Search the web.')
assert.equal(oneLine('No full stop here'), 'No full stop here')
assert.equal(oneLine('a'.repeat(30) + ' ' + 'b'.repeat(200), 60).endsWith('…'), true)
assert.ok(oneLine('word '.repeat(80), 60).length <= 61)
assert.equal(oneLine(undefined), '')
assert.equal(oneLine('Pass a path, e.g. a file. Next one.'), 'Pass a path, e.g. a file.')

console.log('toolGroups ok')
