/* Which live security events interrupt the operator, and which count.
 *
 * The server decides (backend/security.py) and stamps each event with `ping`,
 * `tier` (critical | approval | alert | record), `count` and `repeat`. These
 * are the client's reading of those fields, with fallbacks for an event that
 * lacks them (published by a raise site that bypasses raise_event, or by an
 * older server): only a critical one pings, and info is a record.
 */

export const isCrit = (ev) =>
  ['critical', 'crit'].includes(String(ev.severity || '').toLowerCase())
  || ev.tier === 'critical'

export const wantsPing = (ev) => (ev.ping !== undefined ? !!ev.ping : isCrit(ev))

// info events are records: in the Security log, not in the badge. A repeat is
// already counted (it bumped a row that is in the badge).
export const countsInBadge = (ev) => !ev.repeat
  && (ev.tier ? ev.tier !== 'record' : String(ev.severity || '').toLowerCase() !== 'info')
