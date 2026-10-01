// Resuming a turn that died (a guest crash, a lost connection, a provider
// error). The server saves a dead turn as an assistant row "(turn failed: ...)"
// (older rows say "(guest loop error: ...)") with its tool calls attached, and
// POST /api/chat/<id>/resume sends RESUME_TEXT on, handing the model the steps
// the turn had already run so it picks up from the last one.

// What the resume turn's user message says (backend/chat.py RESUME_MESSAGE).
export const RESUME_TEXT = 'Continue from where the previous turn stopped.'

export const resumeUrl = (id) => `/api/chat/${id}/resume`

const FAILED = /^\s*\((turn failed|guest loop error):/

// A saved failed turn, as the transcript reloads it.
export function isFailedRow(m) {
  return !!m && m.role === 'assistant' && !m.streaming && FAILED.test(m.content || '')
}

// Offer Resume when the LAST message is a failed turn: either the saved row, or
// the error line a stream just ended with (failTurn marks it `failed`; the
// server has saved the failure by then). Anything after it, even a refusal,
// and the offer is gone: the server turns a resume down then too.
export function resumable(messages) {
  const last = messages?.[messages.length - 1]
  return !!last && (isFailedRow(last) || (last.role === 'error' && last.failed === true))
}
