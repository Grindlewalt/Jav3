import { useEffect, useRef } from 'react'

// Close-on-outside-click + Escape for the nav popovers. Every dropdown in the
// bar (bell, triage, VM, More) used to stay open until you clicked its own
// button again, so two of them could sit open on top of each other.
// Usage: const ref = useDismiss(open, () => setOpen(false))
// `alsoInside` is a second ref counted as inside — a popover portalled out of
// the wrapper's DOM subtree (Menu's `floating` mode) still belongs to it.
export function useDismiss(open, onClose, alsoInside = null) {
  const ref = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const onPointer = (e) => {
      if (alsoInside?.current?.contains(e.target)) return
      if (ref.current && !ref.current.contains(e.target)) onClose()
    }
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    // pointerdown (not click) so the menu is gone before the page reacts
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onClose, alsoInside])
  return ref
}
