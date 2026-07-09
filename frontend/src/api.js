export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (res.status === 401) throw new ApiError(401, 'not authenticated')
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* not json */ }
    throw new ApiError(res.status, detail, res)
  }
  return res.json()
}

export class ApiError extends Error {
  constructor(status, detail, res) {
    super(detail)
    this.status = status
    this.detail = detail
    this.res = res
  }
}

async function readSse(res, onEvent) {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      if (chunk.startsWith('data: ')) onEvent(JSON.parse(chunk.slice(6)))
    }
  }
}

// POST /api/chat and invoke onEvent for each SSE event. Throws ApiError on
// non-200 (409 peak_confirmation_required included) before any event fires.
export async function chatStream(body, onEvent, url = '/api/chat') {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch { /* not json */ }
    const err = new ApiError(res.status, detail, res)
    err.conversationId = res.headers.get('X-Conversation-Id')
    throw err
  }
  await readSse(res, onEvent)
}

// GET-tail an SSE stream (re-attach to an in-flight turn after a reload or
// coming back to the tab). The turn runs server-side either way; this watches.
export async function tailStream(url, onEvent, signal) {
  const res = await fetch(url, { signal })
  if (!res.ok) throw new ApiError(res.status, res.statusText, res)
  await readSse(res, onEvent)
}
