import { useCallback, useEffect, useRef, useState } from 'react'
import { api, chatStream, tailStream } from './api.js'
import { makeTurnFolder, newTurn, seedParts } from './turnEvents.js'

// One chat turn, wherever a chat is rendered: the transcript, the busy flag,
// and the resume-tail's AbortController.
//
// Ported from the unmerged v1 consolidation branch (c8a4087), where it was
// written to fold the Chat page's and the ChatBox panel's copied turn
// machinery into one. The shell (/shell) is its first user; Chat.jsx and
// ChatBox.jsx still carry their own copies and can move onto it later.
//
// What is genuinely per-surface stays out: the transcript's chrome, the
// composer, the project/agent/temporary flags that go into the request body,
// and what a `start` event means (the shell adopts the id into the URL unless
// the chat is temporary). Those arrive as the `onEvent` wrapper and the body.
export function useChatStream() {
  const [messages, setMessages] = useState([])
  const [busy, setBusy] = useState(false)
  const tailAbort = useRef(null)   // cancels a resume-tail on switch/unmount
  const openSeq = useRef(0)        // which openThread call is the current one
  const folder = useRef(null)      // folds events into `messages` (batches tokens)
  if (!folder.current) folder.current = makeTurnFolder((fn) => setMessages(fn))

  useEffect(() => () => { tailAbort.current?.abort(); folder.current.cancel() }, [])
  const abortTail = useCallback(() => tailAbort.current?.abort(), [])

  // token/tool/tool_result fold into the streaming message's parts (text
  // tokens batched); final swaps in the reply with the activity collapsed above
  // it; an error keeps the run and appends itself (turnEvents.js).
  const handleTurnEvent = useCallback((ev) => folder.current.handle(ev), [])

  // Load a thread's transcript and, if a turn is still executing server-side,
  // re-attach to it and watch it finish — seeding the placeholder with the tool
  // calls it already made so the activity list isn't missing its first half.
  //
  // `onEvent` replaces handleTurnEvent for callers that need to see `start`
  // during a tail as well as during a send. `onTailDone` runs only when the
  // tail completed rather than being aborted (the page refreshes its list).
  // Resolves to the /messages payload (agent_slug, jobs…), or null when the
  // call was superseded by a later open.
  const openThread = useCallback(async (id, { onEvent, onTailDone } = {}) => {
    tailAbort.current?.abort()
    folder.current.cancel()
    // a slow transcript fetch for the chat the operator already left must not
    // land over the one they switched to
    const mine = ++openSeq.current
    setBusy(false)   // an abandoned send or tail no longer speaks for this view
    if (id == null) { setMessages([]); return null }
    const emit = onEvent || handleTurnEvent
    const r = await api(`/api/conversations/${id}/messages`)
    if (mine !== openSeq.current) return null
    setMessages(r.messages)
    if (!r.running) return r
    setBusy(true)
    const seed = seedParts(r)
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true,
                                parts: seed, t0: Date.now() }])
    const ctl = new AbortController()
    tailAbort.current = ctl
    let settled = false
    try {
      await tailStream(`/api/chat/${id}/stream`, (ev) => {
        if (ev.type === 'idle') {
          // turn ended between the messages fetch and the tail — reload
          settled = true
          api(`/api/conversations/${id}/messages`).then((r2) => {
            if (mine === openSeq.current) setMessages(r2.messages)
          })
          return
        }
        if (ev.type === 'final' || ev.type === 'error') settled = true
        emit(ev)
      }, ctl.signal)
      if (mine === openSeq.current) onTailDone?.()
    } catch { /* tail aborted or dropped */ }
    if (mine !== openSeq.current) return r
    folder.current.flush()
    if (!settled) {
      // ended without an ending (a dropped connection): show what is saved
      // rather than leave a placeholder spinning
      try {
        const r2 = await api(`/api/conversations/${id}/messages`)
        if (mine === openSeq.current) setMessages(r2.messages)
      } catch { /* offline */ }
    }
    if (mine === openSeq.current) setBusy(false)
    return r
  }, [handleTurnEvent])

  // Ends the turn server-side; every tail gets a final "[Request interrupted]"
  // event, so the normal finish path settles the UI.
  const stopTurn = useCallback(async (id) => {
    if (!id) return
    try { await api(`/api/chat/${id}/stop`, { method: 'POST' }) } catch { /* already done */ }
  }, [])

  // Send `text`, streaming the reply into the transcript.
  //
  // The optimistic pair (the user's line + the assistant placeholder) goes in
  // before the request and comes back out on any failure; `onRestoreDraft` puts
  // the text back in the composer.
  const runTurn = useCallback(async ({
    text, body, url, onEvent, onDone, onRestoreDraft,
  }) => {
    // Leaving the chat mid-send (another chat, a new one) bumps openSeq. The
    // turn keeps running server-side — the sidebar shows it and reopening
    // re-attaches — but this stream must stop writing into a transcript that
    // now belongs to a different chat.
    const gen = openSeq.current
    const here = () => gen === openSeq.current
    const emit = onEvent || handleTurnEvent
    let liveId = body?.conversation_id ?? null
    let started = false
    let settled = false
    setBusy(true)
    setMessages((m) => [...m, ...newTurn(text)])
    try {
      await chatStream(body, (ev) => {
        if (!here()) return
        if (ev.type === 'start') { started = true; liveId = ev.conversation_id ?? liveId }
        if (ev.type === 'final' || ev.type === 'error') settled = true
        emit(ev)
      }, url)
    } catch (err) {
      if (!here()) return
      folder.current.flush()
      if (started && !err.status) {
        settled = false   // the connection dropped mid-turn; it goes on server-side
      } else {
        // refused before anything streamed: drop the two optimistic messages
        setMessages((m) => m.slice(0, -2))
        onRestoreDraft?.(text)
        setMessages((m) => [...m, { role: 'error',
          content: err.status === 409 && err.detail === 'turn_in_progress'
            ? 'a turn is still running in this chat — wait for it to finish'
            : (err.detail || String(err)) }])
        setBusy(false)
        return
      }
    }
    if (!here()) return
    folder.current.flush()
    if (!settled && liveId != null) {
      // no ending arrived: pick the turn back up instead of leaving a spinner
      openThread(liveId, { onEvent, onTailDone: onDone })
      return
    }
    onDone?.()
    setBusy(false)
  }, [handleTurnEvent, openThread])

  return {
    messages, setMessages, busy, setBusy,
    handleTurnEvent, openThread, abortTail, stopTurn, runTurn,
  }
}
