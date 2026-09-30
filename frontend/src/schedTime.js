// Schedule times, said in a clock the reader can name (WEBA-11).
//
// The server keeps next_run / last_run as naive SERVER-LOCAL text
// ("2026-09-30T09:00", backend/schedules.py uses datetime.now()) and
// deleted_at as SQLite's UTC ("2026-09-29 20:47:12"). Shown raw, one card
// mixed two clocks and named neither. GET /api/schedules now also says which
// zone the server is in (`server_tz`: { name, utc_offset_min }).

const NAIVE = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/

// naive text read in a zone `offsetMin` minutes east of UTC -> epoch ms
export function naiveToEpoch(s, offsetMin = 0) {
  const m = NAIVE.exec(String(s || ''))
  if (!m) return null
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) - offsetMin * 60000
}

// epoch ms -> "YYYY-MM-DD HH:MM ZONE" in `timeZone` (default: the browser's)
export function fmtZoned(ms, timeZone) {
  if (ms == null || Number.isNaN(ms)) return ''
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
  }).formatToParts(new Date(ms))
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]))
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.timeZoneName}`
}

// A schedule time as the server means it, plus the reader's own clock when it
// differs: "2026-09-30 09:00 BST (01:00 PDT for you)". `tz` is the API's
// server_tz; without it (an older server) the text says "server time".
export function serverTime(s, tz, browserZone) {
  if (!s) return ''
  const naive = String(s).replace('T', ' ').slice(0, 16)
  if (!tz || typeof tz.utc_offset_min !== 'number') return `${naive} server time`
  const label = `${naive} ${tz.name || 'server time'}`
  const mine = fmtZoned(naiveToEpoch(s, tz.utc_offset_min), browserZone)
  // same wall clock in both zones (or the browser can't say): one clock is enough
  if (!mine || mine.slice(0, 16) === naive) return label
  return `${label} (${mine.slice(0, 10) === naive.slice(0, 10) ? mine.slice(11) : mine} for you)`
}

// SQLite's datetime('now') text (UTC, no marker) in the reader's clock
export function utcTime(s, browserZone) {
  const ms = naiveToEpoch(s, 0)
  return ms == null ? '' : fmtZoned(ms, browserZone)
}
