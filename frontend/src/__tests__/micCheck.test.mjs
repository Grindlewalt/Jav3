// node frontend/src/__tests__/micCheck.test.mjs
import assert from 'node:assert/strict'
import { micErrorText, micProblem } from '../micCheck.js'

// https or localhost with a mic API: fine
assert.equal(micProblem({ isSecureContext: true, hasMediaDevices: true,
  protocol: 'https:', host: 'jav3.example' }), null)
assert.equal(micProblem({ isSecureContext: true, hasMediaDevices: true,
  protocol: 'http:', host: 'localhost:5173' }), null)

// the plain-http LAN address: says why, and names the address
const lan = micProblem({ isSecureContext: false, hasMediaDevices: false,
  protocol: 'http:', host: '10.0.0.82:8000' })
assert.match(lan, /only allow it on https or on localhost/)
assert.match(lan, /http:\/\/10\.0\.0\.82:8000/)
assert.match(lan, /https address/)

// a secure page on a browser with no mic API is a different sentence
const none = micProblem({ isSecureContext: true, hasMediaDevices: false,
  protocol: 'https:', host: 'x' })
assert.match(none, /no access to a microphone/)

// a rejected getUserMedia
assert.match(micErrorText({ name: 'NotAllowedError' }), /Allow it for this site/)
assert.match(micErrorText({ name: 'NotFoundError' }), /No microphone was found/)
assert.match(micErrorText(new Error('boom')), /could not be opened: boom/)
assert.match(micErrorText(undefined), /could not be opened/)

console.log('micCheck ok')
