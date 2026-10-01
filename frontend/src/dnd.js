/* Do not disturb, as the browser needs it. The server keeps the state and ends
 * it (backend/security.py); the browser only works out the end time in the
 * operator's own clock, because "until 08:00" is their 08:00, not the Pi's.
 */

// The next 08:00 local: this morning if it is not 08:00 yet, else tomorrow's.
export function nextEight(now = new Date()) {
  const t = new Date(now)
  t.setHours(8, 0, 0, 0)
  if (t <= now) t.setDate(t.getDate() + 1)
  return t
}

// The choices in the select. `morning` is worded for the hour it is now.
export function dndPresets(now = new Date()) {
  const today = nextEight(now).getDate() === now.getDate()
  return [
    { value: '60', label: 'For 1 hour' },
    { value: '240', label: 'For 4 hours' },
    { value: 'morning', label: today ? 'Until 08:00' : 'Until tomorrow 08:00' },
    { value: 'manual', label: 'Until I turn it off' },
  ]
}

// The body for PUT /api/notifications/dnd.
export function dndBody(choice, now = new Date()) {
  if (choice === 'off') return { on: false }
  if (choice === 'manual') return { on: true }
  if (choice === 'morning') return { on: true, until: nextEight(now).toISOString() }
  const minutes = Number(choice)
  if (Number.isInteger(minutes) && minutes > 0) return { on: true, minutes }
  return null
}

const hhmm = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })

// "until 08:00", "until Fri 08:00", "until I turn it off"
export function dndUntil(dnd, now = new Date()) {
  if (!dnd || !dnd.until) return 'until I turn it off'
  const d = new Date(dnd.until)
  if (Number.isNaN(d.getTime())) return 'until I turn it off'
  const sameDay = d.toDateString() === now.toDateString()
  const day = sameDay ? '' : `${d.toLocaleDateString([], { weekday: 'short' })} `
  return `until ${day}${hhmm(d)}`
}

// A short time left for the top bar: "47m", "1h 12m", "" when open-ended.
export function dndLeft(dnd, now = new Date()) {
  if (!dnd || !dnd.until) return ''
  const mins = Math.max(0, Math.ceil((new Date(dnd.until) - now) / 60000))
  if (!Number.isFinite(mins)) return ''
  if (mins < 60) return `${mins}m`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return m ? `${h}h ${m}m` : `${h}h`
}
