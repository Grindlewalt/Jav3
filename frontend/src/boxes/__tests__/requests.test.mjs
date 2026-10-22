// node frontend/src/boxes/__tests__/requests.test.mjs
import assert from 'node:assert/strict'
import { packagesByManager, skillCardState } from '../logic.js'

const g = packagesByManager([{ manager: 'apt', package: 'a' }, { manager: 'pip', package: 'p' },
  { manager: 'apt', package: 'b' }])
assert.deepEqual(g.map(([m, l]) => [m, l.map((x) => x.package)]), [['apt', ['a', 'b']], ['pip', ['p']]])
assert.deepEqual(packagesByManager(null), [])

const pend = skillCardState({ status: 'pending' })
assert.ok(pend.canFetch && !pend.canInstall && pend.canReject)
const f = skillCardState({ status: 'fetched', skill_sha256: 'ab', files: { 'SKILL.md': 'ab', 'x.md': 'c' },
  requirements: [{ reason: 'needs ffmpeg', met: false }, { reason: 'ok', met: true }] })
assert.ok(f.canInstall && f.canRefetch && !f.canFetch)
assert.equal(f.unmet.length, 1)
assert.deepEqual(f.files, ['SKILL.md', 'x.md'])
assert.equal(skillCardState({ status: 'fetched' }).canInstall, false)
const inst = skillCardState({ status: 'installed', granted: false })
assert.ok(inst.notGranted && !inst.canReject)
assert.equal(skillCardState({ status: 'installed', granted: true }).notGranted, false)
console.log('requests ok')
