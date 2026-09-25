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

// How long ago, coarse: "just now", "5m ago", "3h ago", "2d ago", then the
// date. SQLite's datetime('now') is UTC with no zone marker, so a bare
// "YYYY-MM-DD HH:MM:SS" is read as UTC, not as the browser's local time.
export function ago(s, now = Date.now()) {
  if (!s) return ''
  const iso = String(s).replace(' ', 'T')
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  if (Number.isNaN(t)) return ts(s)
  const sec = Math.max(0, (now - t) / 1000)
  if (sec < 60) return 'just now'
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)}d ago`
  return ts(s).slice(0, 10)
}

// A live count for a badge. Counts come from real queues and reached 294 in
// practice, which overflowed the nav's pill and smeared across the icon.
export const badge = (n) => (n > 99 ? '99+' : String(n))

