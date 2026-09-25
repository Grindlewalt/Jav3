import { useEffect, useState } from 'react'

// The app's breakpoints, in one place.
//
// There were four, uncoordinated: 1024, 768, 640 and 400. 640 carried exactly
// two rules — the modal going full-bleed and the music player going
// edge-to-edge — and both are "this is a phone" decisions that the rest of the
// app already spells 768 (the SecurityBoard modal goes full-bleed at 768, so
// the app had two different widths for the same idea). 640 is gone; three
// remain, and each one means something you can say in a sentence:
//
//   wide    992  the top bar can no longer hold the links; they move to the drawer
//   phone   768  one-column layouts, 16px controls, safe-area gutters
//   narrow  400  the last squeeze: smaller wordmark, tighter page padding
//
// CSS custom properties cannot be used inside @media, so styles.css still
// writes the numbers literally — under a comment pointing here, and every one
// of them is one of these three. This module is the source for JS; `--bp-*`
// in :root is the source a devtools inspection can read.
export const BP = {
  // measured, not a device width: brand + six links (Review wearing its
  // count) + ⋯ + the VM/voice/theme cluster need ~966px; 992 keeps a gutter
  // of spare room. It was 1024, which sent a bar with ~170px to spare at
  // 1100 into the drawer.
  wide: 992,
  phone: 768,
  narrow: 400,
}

// Media-query strings, so a call site never re-spells the number. Chat.jsx
// used to carry `(max-width: 768px)` four separate times.
export const WIDE_QUERY = `(max-width: ${BP.wide}px)`
export const PHONE_QUERY = `(max-width: ${BP.phone}px)`
export const NARROW_QUERY = `(max-width: ${BP.narrow}px)`

// Answers "is this a phone right now?" without a subscription — for the inside
// of an event handler or a state initialiser, where a hook cannot go.
export const isPhone = () => window.matchMedia(PHONE_QUERY).matches

// Subscribing form. Chat.jsx kept this as a useState seed plus a useEffect that
// added and removed the listener by hand, beside two more bare matchMedia
// reads; all four call sites are this module now.
export function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const onChange = (e) => setMatches(e.matches)
    setMatches(mq.matches)   // the width may have moved between renders
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [query])
  return matches
}

export const useIsPhone = () => useMediaQuery(PHONE_QUERY)
