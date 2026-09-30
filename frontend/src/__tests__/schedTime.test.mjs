// node frontend/src/__tests__/schedTime.test.mjs
import assert from 'node:assert/strict'
import { fmtZoned, naiveToEpoch, serverTime, utcTime } from '../schedTime.js'

let n = 0
const t = (name, fn) => { fn(); n += 1; console.log('ok', name) }
const LA = 'America/Los_Angeles'
const BST = { name: 'BST', utc_offset_min: 60 }

t('naive server text reads in the server zone, not the reader\'s', () => {
  // 09:00 BST is 08:00 UTC
  assert.equal(naiveToEpoch('2026-09-30T09:00', 60), Date.UTC(2026, 8, 30, 8, 0))
  assert.equal(naiveToEpoch('2026-09-30 09:00:00', 0), Date.UTC(2026, 8, 30, 9, 0))
  assert.equal(naiveToEpoch('nonsense'), null)
  assert.equal(naiveToEpoch(''), null)
})
t('a server time is named, with the reader\'s clock when it differs', () => {
  assert.equal(serverTime('2026-09-30T09:00', BST, LA), '2026-09-30 09:00 BST (01:00 PDT for you)')
  // it lands on the previous day for the reader: the date is shown
  assert.equal(serverTime('2026-09-30T02:00', BST, LA), '2026-09-30 02:00 BST (2026-09-29 18:00 PDT for you)')
  // same wall clock: one clock is enough
  assert.equal(serverTime('2026-09-30T09:00', BST, 'Europe/London'), '2026-09-30 09:00 BST')
  assert.equal(serverTime('', BST, LA), '')
})
t('an older server (no server_tz) is still called server time', () => {
  assert.equal(serverTime('2026-09-30T09:00', undefined, LA), '2026-09-30 09:00 server time')
})
t('the trash time is UTC and shown in the reader\'s zone', () => {
  assert.equal(utcTime('2026-09-29 20:47:12', LA), '2026-09-29 13:47 PDT')
  assert.equal(utcTime(null, LA), '')
})
t('fmtZoned takes an epoch', () => {
  assert.equal(fmtZoned(Date.UTC(2026, 0, 5, 12, 0), LA), '2026-01-05 04:00 PST')
})
console.log(`${n} passed`)
