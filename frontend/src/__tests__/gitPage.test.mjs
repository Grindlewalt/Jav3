// node frontend/src/__tests__/gitPage.test.mjs
import assert from 'node:assert/strict'
import {
  accessEditable, diffLines, diffStat, filesText, pickSlug, projectOptions, shortDate,
  syncView, urlNote,
} from '../gitPage.js'

// the push status row says what is true and offers only what is safe
const full = 'a79af3f1111111111111111111111111111111aa'
const sv = (o) => syncView({ host: '295e89b' + full.slice(7), gitea: full, ahead: 0, behind: 0, ...o })
assert.equal(sv({ state: 'in_sync', gitea: '295e89b' + full.slice(7) }).text, 'host main 295e89b = Gitea main')
assert.equal(sv({ state: 'in_sync' }).tone, 'done')
assert.equal(sv({ state: 'in_sync' }).action, null)
const ahead = sv({ state: 'host_ahead', ahead: 2 })
assert.equal(ahead.text, 'host main 295e89b is 2 commits ahead of Gitea main a79af3f')
assert.equal(ahead.action.label, 'Push main')
assert.match(sv({ state: 'host_ahead', ahead: 1 }).text, /is 1 commit ahead/)
assert.equal(sv({ state: 'host_ahead', gitea: null }).text, 'host main 295e89b; Gitea has no main yet')
const behind = sv({ state: 'gitea_ahead', behind: 3 })
assert.equal(behind.text, 'Gitea main a79af3f is 3 commits ahead of host main 295e89b')
assert.equal(behind.action.label, 'Update host')
assert.match(behind.action.title, /working files are not touched/)
const div = sv({ state: 'diverged', ahead: 1, behind: 2 })
assert.equal(div.action, null)
assert.equal(div.tone, 'error')
assert.match(div.text, /1 ahead, 2 behind/)
assert.equal(sv({ state: 'empty' }).action, null)
assert.equal(sv({ state: 'unknown', error: 'fetch from Gitea failed: x' }).text, 'fetch from Gitea failed: x')
assert.equal(syncView(null), null)

// the +/- and the files count come from the request row, and may be absent
assert.equal(diffStat({ added: 120, removed: 4 }), '+120 −4')
assert.equal(diffStat({ added: 5, removed: null }), '+5 −0')
assert.equal(diffStat({ added: null }), '')
assert.equal(diffStat(null), '')
assert.equal(filesText({ files: 1 }), '1 file')
assert.equal(filesText({ files: 3 }), '3 files')
assert.equal(filesText({ files: null }), '')

// the date is the author's own, offset and all
assert.equal(shortDate('2026-10-01T15:52:00+01:00'), '2026-10-01')
assert.equal(shortDate('2026-09-27T23:59:00-08:00'), '2026-09-27')
assert.equal(shortDate(''), '')
assert.equal(shortDate(undefined), '')

// only a person's grant can be changed here
assert.equal(accessEditable({ login: 'x', permission: 'read' }), true)
assert.equal(accessEditable({ login: 'x', permission: 'none' }), true)
assert.equal(accessEditable({ login: 'x', permission: 'write' }), true)
assert.equal(accessEditable({ login: 'o', permission: 'owner', owner: true }), false)
assert.equal(accessEditable({ login: 'b', permission: 'write', bot: true }), false)
assert.equal(accessEditable({ login: 'x', permission: 'admin' }), false)

// which project opens first
const projects = [{ slug: 'alpha', name: 'Alpha' }, { slug: 'beta', name: 'Beta' },
  { slug: 'gamma', name: 'Gamma' }]
const repos = [{ slug: 'beta', open_prs: 0 }, { slug: 'gamma', open_prs: 2 }]
assert.equal(pickSlug({ route: 'beta', remembered: 'gamma', projects, repos }), 'beta')
assert.equal(pickSlug({ route: 'nope', remembered: 'beta', projects, repos }), 'beta')
assert.equal(pickSlug({ route: null, remembered: 'nope', projects, repos }), 'gamma')   // a PR waits
assert.equal(pickSlug({ projects, repos: [{ slug: 'beta', open_prs: 0 }] }), 'beta')
assert.equal(pickSlug({ projects, repos: [] }), 'alpha')
assert.equal(pickSlug({ projects: [], repos: [] }), null)
assert.equal(pickSlug({ route: 'orphan', projects, repos: [{ slug: 'orphan', open_prs: 0 }] }), 'orphan')

// the project select marks the ones with no repo and keeps a repo that is no project
assert.deepEqual(projectOptions(projects, repos), [
  { value: 'alpha', label: 'Alpha (no repo yet)' },
  { value: 'beta', label: 'Beta' },
  { value: 'gamma', label: 'Gamma' },
])
assert.deepEqual(projectOptions([], [{ slug: 'old' }]), [{ value: 'old', label: 'old (not a project)' }])

// the address note shows only while the links are the LAN default
assert.equal(urlNote({ configured: true, url_configured: true, url: 'https://git.example' }), '')
assert.equal(urlNote(null), '')
assert.equal(urlNote({ configured: false, url_configured: false }), '')
const note = urlNote({ configured: true, url_configured: false, url: 'http://10.0.0.82:3000' })
assert.match(note, /http:\/\/10\.0\.0\.82:3000/)
assert.match(note, /JARVIS_GITEA_URL/)

// a diff paints its file headers, hunks, adds and removes; `-- sql comment` is a removal
const d = diffLines(['diff --git a/x.py b/x.py', 'index 1..2 100644', '--- a/x.py', '+++ b/x.py',
  '@@ -1,2 +1,2 @@', ' same', '-old', '+new', '--- a comment', ''].join('\n'))
assert.deepEqual(d.map((l) => l.cls),
  ['file', 'file', 'file', 'file', 'hunk', '', 'del', 'add', 'del', ''])
assert.equal(d[9].text, ' ')

console.log('gitPage ok')
