import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { atBottom } from './turnEvents.js'

// Keep a scroller at its end while a turn streams into it, unless the reader
// has scrolled up. The transcript used to snap to the bottom on every update,
// so reading an earlier message during a run was a fight.
//
//   ref     the scroll container
//   mark    a number that grows when something new appears (turnEvents.
//           activityMark); the "N new" pill counts the growth since the
//           reader left the end
//   resetKey  changes when the transcript is a different one (another chat):
//           follow again from the top of it
//
// Returns { pinned, away, follow, jump }: `pinned` = following; `away` = new
// things since the reader left; `follow()` = call after an update that grew
// the content (a no-op while the reader is elsewhere); `jump()` = back to the
// end and following again (a send, or the pill).
//
// "Left" means scrolled UP, or opened/expanded something to read it. Not merely
// "not at the bottom": a burst of new rows lands before the scroll catches up
// and looks exactly like that.
export function useFollow(ref, { mark = 0, resetKey = null } = {}) {
  const stuck = useRef(true)
  const last = useRef(0)
  const leftAt = useRef(mark)
  const markRef = useRef(mark)
  markRef.current = mark
  const [pinned, setPinned] = useState(true)

  const toEnd = useCallback(() => {
    const el = ref.current
    if (!el || !el.clientHeight) return   // hidden (the Runs view stands in)
    el.scrollTop = el.scrollHeight
    last.current = el.scrollTop
  }, [ref])

  const leave = useCallback(() => {
    if (!stuck.current) return
    stuck.current = false
    leftAt.current = markRef.current
    setPinned(false)
  }, [])
  const rejoin = useCallback(() => {
    if (stuck.current) return
    stuck.current = true
    setPinned(true)
  }, [])

  const follow = useCallback(() => { if (stuck.current) toEnd() }, [toEnd])
  const jump = useCallback(() => { rejoin(); toEnd() }, [rejoin, toEnd])

  // scrolls and disclosure clicks decide whether the reader has left; growth
  // that arrives without either (rows, job trees, images) is followed
  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    last.current = el.scrollTop
    const onScroll = () => {
      const top = el.scrollTop
      if (atBottom(top, el.clientHeight, el.scrollHeight)) rejoin()
      else if (top < last.current - 2) leave()
      last.current = top
    }
    const onClick = (e) => { if (e.target.closest?.('[aria-expanded]')) leave() }
    el.addEventListener('scroll', onScroll, { passive: true })
    el.addEventListener('click', onClick)
    const ro = new ResizeObserver(() => { if (stuck.current) toEnd() })
    ro.observe(el)
    // whatever the transcript is drawn into: the thread, the empty state, the
    // compact list. Re-pointed when the container's children change.
    const watch = () => { for (const c of el.children) ro.observe(c) }
    watch()
    const mo = new MutationObserver(watch)
    mo.observe(el, { childList: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      el.removeEventListener('click', onClick)
      ro.disconnect()
      mo.disconnect()
    }
  }, [ref, leave, rejoin, toEnd])

  // another transcript: start following it
  useLayoutEffect(() => { stuck.current = true; setPinned(true); toEnd() }, [resetKey, toEnd])

  return { pinned, away: pinned ? 0 : Math.max(0, mark - leftAt.current), follow, jump }
}

// The composer floats over the bottom of the thread, and what rides with it
// (the status line, an open question) changes its height. Publish that height
// as --dock-h on its parent so the thread's bottom padding always clears it.
export function useDockHeight(ref) {
  useLayoutEffect(() => {
    const el = ref.current
    const host = el?.parentElement
    if (!el || !host) return undefined
    const write = () => host.style.setProperty('--dock-h', `${el.offsetHeight}px`)
    write()
    const ro = new ResizeObserver(write)
    ro.observe(el)
    return () => { ro.disconnect(); host.style.removeProperty('--dock-h') }
  }, [ref])
}
