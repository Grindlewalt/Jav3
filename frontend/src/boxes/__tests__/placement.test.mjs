// node frontend/src/boxes/__tests__/placement.test.mjs
import assert from 'node:assert/strict'
import {
  effectiveText, inUseBoxes, memError, newBoxBody, pickerItems, pickOutcome, separateBody,
} from '../placement.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }

const BOXES = [
  { id: 'shared', kind: 'shared', owner: null, runtime: 'kvm', image: 'main', state: 'running',
    used_by: ['osint', 'tba'], in_use: true },
  { id: 'p-minecraft', kind: 'project', owner: 'minecraft', runtime: 'kvm', image: 'dev',
    state: 'running', used_by: ['minecraft'], in_use: true },
  { id: 'p-countdown', kind: 'project', owner: 'countdown', runtime: 'docker', image: 'main',
    state: 'stopped', used_by: [], in_use: false },
]
const IMAGES = [{ name: 'main' }, { name: 'dev' }, { name: 'desktop' }]

t('inUseBoxes keeps only boxes in use', () => {
  assert.deepEqual(inUseBoxes(BOXES).map((b) => b.id), ['shared', 'p-minecraft'])
  assert.deepEqual(inUseBoxes(null), [])
})

t('pickerItems filters boxes and images together', () => {
  const all = pickerItems(BOXES, IMAGES, '')
  assert.equal(all.length, 6)
  const dev = pickerItems(BOXES, IMAGES, 'dev')
  assert.deepEqual(dev.map((i) => i.key), ['box:p-minecraft', 'image:dev'])
  assert.equal(dev[1].running, true)
  assert.deepEqual(pickerItems(BOXES, IMAGES, 'tba').map((i) => i.key), ['box:shared'])
  assert.deepEqual(pickerItems(BOXES, IMAGES, 'container').map((i) => i.key), ['box:p-countdown'])
  assert.equal(pickerItems(BOXES, IMAGES, 'desk')[0].running, false)
})

t('pickOutcome: current, shared, own, and ask for another project\'s box', () => {
  const eff = { box_id: 'shared' }
  assert.equal(pickOutcome(BOXES[0], 'arm', eff).kind, 'current')
  assert.deepEqual(pickOutcome(BOXES[0], 'arm', { box_id: 'p-arm' }).body, { mode: 'shared' })
  const own = pickOutcome({ ...BOXES[2], id: 'p-arm', owner: 'arm' }, 'arm', eff)
  assert.equal(own.kind, 'own')
  const ask = pickOutcome(BOXES[1], 'arm', eff)
  assert.equal(ask.kind, 'ask')
  assert.deepEqual(ask.users, ['minecraft'])
  assert.deepEqual(ask.share, { mode: 'join', box_id: 'p-minecraft' })
  assert.deepEqual(ask.separate, { mode: 'own', runtime: 'kvm', image: 'dev', mem_mb: null })
  // an unused box still names its owner
  assert.deepEqual(pickOutcome(BOXES[2], 'arm', eff).users, ['countdown'])
  assert.equal(pickOutcome(null, 'arm', eff).kind, 'none')
})

t('separateBody / newBoxBody carry image, runtime and memory', () => {
  assert.deepEqual(separateBody(BOXES[2], '512'),
    { mode: 'own', runtime: 'docker', image: 'main', mem_mb: 512 })
  assert.deepEqual(newBoxBody({ runtime: 'docker', image: 'dev', mem: '' }),
    { mode: 'own', runtime: 'docker', image: 'dev', mem_mb: null })
  assert.equal(newBoxBody({ runtime: 'weird' }).runtime, 'kvm')
})

t('memError', () => {
  assert.equal(memError(''), null)
  assert.equal(memError('512'), null)
  assert.ok(memError('12'))
  assert.ok(memError('1.5'))
})

t('effectiveText', () => {
  assert.equal(effectiveText({ mode: 'shared', source: 'profile' }, 'Default'),
    'the shared box (default of the Default profile)')
  assert.equal(effectiveText({ mode: 'join', box_id: 'p-mc', owner: 'mc', source: 'project' }),
    "p-mc, mc's box (shared with it)")
  assert.match(effectiveText({ mode: 'own', box_id: 'p-a', runtime: 'docker', image: 'dev',
    mem_mb: 600, source: 'project' }), /own container p-a · image dev · 600 MB$/)
})

console.log(`${n} placement tests passed`)
