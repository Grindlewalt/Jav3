// What belongs in the Review Queue. The list the page loads is already cut on the
// server (`?queue=true`: no record-tier rows, the audit lines that stay in the
// history); this is the same rule for a row that arrives live.
//
// A live security event carries `tier` (what the kind usually is) and `mode` (what
// THIS operator's choices make of it: ping | badge | record). A row is a Queue
// item unless it is filed already acknowledged, or its mode is record (an audit
// line: it stays in the history). Critical rows are always Queue items, and an
// agent's report (harness_fault) has a section of its own.

export const QUEUE_KEEPS = ['harness_fault']

export function liveInQueue(ev) {
  if (!ev || ev.acknowledged) return false
  if (ev.tier === 'critical' || QUEUE_KEEPS.includes(ev.kind)) return true
  return ev.mode !== 'record'          // an older server sends no mode: it is a Queue item
}
