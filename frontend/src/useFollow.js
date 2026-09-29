import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { atBottom, readerLeft } from './turnEvents.js'

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
// "Left" means the READER scrolled up (a wheel, a touch, a key, the scrollbar,
// and the scroll that follows), or opened/expanded something to read it. Not
// "the position went up": folding old rows into one line shrinks the content
// and moves the scroll position with no one touching it, and a burst of rows
// lands before the scroll catches up and looks exactly like being away.
export function useFollow(ref, { mark = 0, resetKey = null } = {}) {
  const stuck = useRef(true)
  const last = useRef(0)          // where the scroller was when we last looked
  const input = useRef(-Infinity) // when the reader last touched it
  const leftAt = useRef(mark)
  const markRef = useRef(mark)
  markRef.current = mark
  const [pinned, setPinned] = useState(true)
  const followRef = useRef(() => {})

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

  // Follow, unless the reader has gone up. A scroll event reaches us a frame
  // after the scroll, and growth can land in between: following blindly there
  // undid the reader's scroll, and the event then saw no movement at all. So
  // the position is checked first, but only trusted right after real input.
  const follow = useCallback(() => {
    const el = ref.current
    if (!el) return
    if (stuck.current && readerLeft({
      top: el.scrollTop, last: last.current, sinceInput: performance.now() - input.current,
      bottom: atBottom(el.scrollTop, el.clientHeight, el.scrollHeight),
    })) leave()
    if (stuck.current) toEnd()
  }, [ref, leave, toEnd])
  followRef.current = follow
  const jump = useCallback(() => { rejoin(); toEnd() }, [rejoin, toEnd])

  useEffect(() => {
    const el = ref.current
    if (!el) return undefined
    last.current = el.scrollTop
    const touched = () => { input.current = performance.now() }
    const onScroll = () => {
      const top = el.scrollTop
      const bottom = atBottom(top, el.clientHeight, el.scrollHeight)
      if (bottom) rejoin()
      else if (readerLeft({ top, last: last.current, bottom,
                            sinceInput: performance.now() - input.current })) leave()
      last.current = top
    }
    const onClick = (e) => { if (e.target.closest?.('[aria-expanded]')) leave() }
    // wheeling up is leaving, before the scroll event says so (not inside a
    // result that scrolls on its own)
    const onWheel = (e) => {
      touched()
      if (e.deltaY < 0 && el.scrollHeight > el.clientHeight
          && !e.target.closest?.('.tool-pre, .ask-detail, .md-body pre')) leave()
    }
    const opts = { passive: true }
    el.addEventListener('scroll', onScroll, opts)
    el.addEventListener('click', onClick)
    el.addEventListener('wheel', onWheel, opts)
    el.addEventListener('touchstart', touched, opts)
    el.addEventListener('touchmove', touched, opts)
    el.addEventListener('pointerdown', touched, opts)   // the scrollbar too
    el.addEventListener('keydown', touched)
    // growth that arrives on its own (rows, job trees, images) is followed
    const ro = new ResizeObserver(() => { followRef.current() })
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
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('touchstart', touched)
      el.removeEventListener('touchmove', touched)
      el.removeEventListener('pointerdown', touched)
      el.removeEventListener('keydown', touched)
      ro.disconnect()
      mo.disconnect()
    }
  }, [ref, leave, rejoin])

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
