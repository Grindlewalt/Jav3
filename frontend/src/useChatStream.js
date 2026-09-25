import { useCallback, useEffect, useRef, useState } from 'react'
import { api, chatStream, tailStream } from './api.js'
import { applyTurnEvent, finishTurn } from './ToolActivity.jsx'

// One chat turn, wherever a chat is rendered: the transcript, the busy flag,
// the parked peak-pricing draft and the resume-tail's AbortController.
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
  // draft parked on a peak-pricing 409 until the operator answers in-page.
  // This must NOT be window.confirm: the iOS home-screen app suppresses
  // blocking dialogs, so confirm() returns false without ever showing and
  // every send silently bounced back into the bar.
  const [peakAsk, setPeakAsk] = useState(null)
  const tailAbort = useRef(null)   // cancels a resume-tail on switch/unmount
  const openSeq = useRef(0)        // which openThread call is the current one

  useEffect(() => () => tailAbort.current?.abort(), [])
  const abortTail = useCallback(() => tailAbort.current?.abort(), [])

  // token/tool/tool_result fold into the streaming message's parts; final
  // swaps in the reply with the activity collapsed above it.
  const handleTurnEvent = useCallback((ev) => {
    if (['token', 'tool', 'tool_result', 'job'].includes(ev.type))
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = applyTurnEvent(copy[copy.length - 1], ev)
        return copy
      })
    if (ev.type === 'final')
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = finishTurn(copy[copy.length - 1], ev.content)
        return copy
      })
    if (ev.type === 'error')
      setMessages((m) => {
        const copy = [...m]
        copy[copy.length - 1] = { role: 'error', content: ev.message }
        return copy
      })
  }, [])

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
    setPeakAsk(null)
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
    const seed = (r.pending_activity || []).map((a) => ({ kind: 'tool', ...a }))
    setMessages((m) => [...m, { role: 'assistant', content: '', streaming: true, parts: seed }])
    const ctl = new AbortController()
    tailAbort.current = ctl
    try {
      await tailStream(`/api/chat/${id}/stream`, (ev) => {
        if (ev.type === 'idle') {
          // turn ended between the messages fetch and the tail — reload
          api(`/api/conversations/${id}/messages`).then((r2) => setMessages(r2.messages))
          return
        }
        emit(ev)
      }, ctl.signal)
      onTailDone?.()
    } catch { /* tail aborted or dropped; messages reload on next open */ }
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
  // the text back in the composer, which every failure but the peak gate wants.
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
    setBusy(true)
    setMessages((m) => [...m, { role: 'user', content: text },
                        { role: 'assistant', content: '', streaming: true, parts: [] }])
    try {
      await chatStream(body, (ev) => { if (here()) emit(ev) }, url)
      if (here()) onDone?.()
    } catch (err) {
      if (!here()) return
      // drop the two optimistic messages; a peak-retry re-adds them
      setMessages((m) => m.slice(0, -2))
      if (err.status === 409 && err.detail === 'peak_confirmation_required') {
        // a new conversation doesn't exist yet on this 409 (the backend
        // gates before creating it), so the confirmed retry re-sends the
        // parked draft from scratch
        setPeakAsk(text)
      } else if (err.status === 409 && err.detail === 'turn_in_progress') {
        onRestoreDraft?.(text)
        setMessages((m) => [...m, { role: 'error',
          content: 'a turn is still running in this chat — wait for it to finish' }])
      } else {
        onRestoreDraft?.(text)
        setMessages((m) => [...m, { role: 'error', content: err.detail || String(err) }])
      }
    }
    if (here()) setBusy(false)
  }, [handleTurnEvent])

  return {
    messages, setMessages, busy, setBusy, peakAsk, setPeakAsk,
    handleTurnEvent, openThread, abortTail, stopTurn, runTurn,
  }
}
