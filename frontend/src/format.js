// The formatters the pages kept re-declaring.
//
// `human` was written twice, identically, in Network.jsx and Logs.jsx, and a
// third time in SecurityBoard.jsx as `fmtBytes` — same logic, except it emitted
// `kB` where the other two emit `KB` and divided by the literal 1048576. So the
// app showed two different byte units depending on which page you were on.
// `KB` wins: it was two call sites out of three, and it matches the `MB` both
// spellings already agreed on.
//
// fmtBytes also answered '' for a null size, which is load-bearing at its one
// call site (a directory row has no size). That guard belongs at the call site,
// not in the formatter — `human(undefined)` is '0 B' on the other two pages and
// must stay that way.
export function human(n) {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / (1024 * 1024)).toFixed(1)} MB`
}

// Severity -> the CSS modifier. Declared identically in Review.jsx and
// SecurityBoard.jsx, which is exactly the pair that has to agree: the board is
// the evidence behind the card.
export const SEV = {
  info: 'info', warn: 'warn', warning: 'warn', critical: 'crit', crit: 'crit',
}
export const sevClass = (s) => SEV[String(s || 'info').toLowerCase()] || 'info'

// An ISO timestamp, trimmed. Two variants, because the two call sites genuinely
// want different things and folding them into one would change what a page
// prints: Review has room for the date, the triage queue's rows do not.
//   ts      2026-08-29 14:03
//   tsShort 08-29 14:03
export function ts(s) {
  return s ? String(s).replace('T', ' ').slice(0, 16) : ''
}
export function tsShort(s) {
  return s ? String(s).replace('T', ' ').slice(5, 16) : ''
}
