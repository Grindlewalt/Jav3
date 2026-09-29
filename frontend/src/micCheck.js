// Why the Voice page cannot use the microphone, in words (WEB-10).
//
// getUserMedia exists only in a secure context: https, or localhost. Jav3 is
// often opened at a plain-http LAN address, where navigator.mediaDevices is
// undefined and the page used to say "say hey Jav3" anyway, with a mute button
// for a microphone that could never work. Pure functions, tested in
// src/__tests__/micCheck.test.mjs.

// env: {isSecureContext, hasMediaDevices, protocol, host}. null = the mic can work.
export function micProblem(env) {
  if (env.hasMediaDevices && env.isSecureContext !== false) return null
  if (env.isSecureContext === false || env.protocol === 'http:') {
    return `This page cannot use the microphone. Browsers only allow it on https or on `
      + `localhost, and this page was opened at ${env.protocol}//${env.host}. `
      + 'Open Jav3 through an https address, for example the Cloudflare tunnel, and come back to Voice.'
  }
  return 'This browser gives the page no access to a microphone.'
}

// A rejected getUserMedia call, as a sentence.
export function micErrorText(e) {
  const name = e && e.name
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'The browser blocked the microphone. Allow it for this site in the address bar, '
      + 'then reload.'
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return 'No microphone was found on this device.'
  }
  return `The microphone could not be opened: ${(e && e.message) || e}`
}

// The environment the page runs in, for micProblem
export const micEnv = () => ({
  isSecureContext: window.isSecureContext,
  hasMediaDevices: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
  protocol: window.location.protocol,
  host: window.location.host,
})
