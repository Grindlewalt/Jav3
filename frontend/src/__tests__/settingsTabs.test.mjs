// node frontend/src/__tests__/settingsTabs.test.mjs
import assert from 'node:assert/strict'
import {
  SETTINGS_TABS, hashId, settingsRedirect, tabForHash, tabForPath, tabPath,
} from '../settingsTabs.js'

// four tabs, Models the index
assert.deepEqual(SETTINGS_TABS.map((t) => t.id), ['models', 'alerts', 'access', 'system'])
assert.deepEqual(SETTINGS_TABS.map((t) => t.to),
  ['/settings', '/settings/alerts', '/settings/access', '/settings/system'])
assert.equal(SETTINGS_TABS.filter((t) => t.end).length, 1)

// the pathname names the tab; the bare /settings is Models
assert.equal(tabForPath('/settings'), 'models')
assert.equal(tabForPath('/settings/'), 'models')
assert.equal(tabForPath('/settings/alerts'), 'alerts')
assert.equal(tabForPath('/settings/access/'), 'access')
assert.equal(tabForPath('/settings/system'), 'system')
assert.equal(tabForPath('/settings/nonsense'), 'models')
assert.equal(tabPath('alerts'), '/settings/alerts')
assert.equal(tabPath('models'), '/settings')
assert.equal(tabPath('nope'), '/settings')

// every card id the old page carried lands on the tab that holds it now
const where = {
  providers: 'models', notifications: 'alerts', devices: 'access', desk: 'access',
  browser: 'access', grounding: 'access', rules: 'access',
  backup: 'system', music: 'system', session: 'system',
}
for (const [id, tab] of Object.entries(where)) assert.equal(tabForHash(`#${id}`), tab, id)
assert.equal(tabForHash('#NOTIFICATIONS'), 'alerts')      // case does not matter
assert.equal(tabForHash('desk'), 'access')                // with or without the #
assert.equal(tabForHash('#alerts'), 'alerts')             // a tab's own name
assert.equal(tabForHash('#nothing-here'), null)
assert.equal(tabForHash(''), null)
assert.equal(tabForHash(undefined), null)
assert.equal(hashId('#Desk'), 'desk')

// the old links: /settings#notifications, #desk (the bell's shell ask), #providers
// (Grounding's copy, from the Access tab)
assert.equal(settingsRedirect('/settings', '#notifications'), '/settings/alerts#notifications')
assert.equal(settingsRedirect('/settings', '#desk'), '/settings/access#desk')
assert.equal(settingsRedirect('/settings/access', '#providers'), '/settings#providers')
assert.equal(settingsRedirect('/settings/access', '#grounding'), null)   // already there
assert.equal(settingsRedirect('/settings/alerts', '#notifications'), null)
assert.equal(settingsRedirect('/settings', '#providers'), null)
assert.equal(settingsRedirect('/settings', ''), null)
assert.equal(settingsRedirect('/settings/system', '#nothing-here'), null)

console.log('settingsTabs ok')
