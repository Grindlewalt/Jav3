import { useEffect, useRef } from 'react'

// Close-on-outside-click + Escape for the nav popovers. Every dropdown in the
// bar (bell, triage, VM, More) used to stay open until you clicked its own
// button again, so two of them could sit open on top of each other.
// Usage: const ref = useDismiss(open, () => setOpen(false))
// `alsoInside` is a second ref counted as inside — a popover portalled out of
// the wrapper's DOM subtree (Menu's `floating` mode) still belongs to it.
//
// Escape closes ONE layer: the most recently opened. Each open layer joins a
// stack, and a single capture-phase listener on window hands the key to the
// top one and swallows it (preventDefault + stopPropagation), so the phone's
// sheet/drawer handlers — plain document listeners — never see the Escape
// that closed a menu inside them. Before, every open layer listened for
// itself and one keypress shut the row ⋯ menu and the sheet under it.
const layers = []

function onEscape(e) {
  if (e.key !== 'Escape' || e.isComposing || !layers.length) return
  e.preventDefault()
  e.stopPropagation()
  layers[layers.length - 1].current()
}

export function useDismiss(open, onClose, alsoInside = null) {
  const ref = useRef(null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    if (!open) return undefined
    const onPointer = (e) => {
      if (alsoInside?.current?.contains(e.target)) return
      if (ref.current && !ref.current.contains(e.target)) close.current()
    }
    const layer = close
    if (!layers.length) window.addEventListener('keydown', onEscape, true)
    layers.push(layer)
    // pointerdown (not click) so the menu is gone before the page reacts
    document.addEventListener('pointerdown', onPointer)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      const i = layers.lastIndexOf(layer)
      if (i !== -1) layers.splice(i, 1)
      if (!layers.length) window.removeEventListener('keydown', onEscape, true)
    }
  }, [open, alsoInside])
  return ref
}
