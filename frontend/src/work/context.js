import { createContext } from 'react'

// STAND-IN. The Work page (built on another branch) owns this file and
// replaces it: the split-panel "windows" beside the chat. Same shape, default
// no-ops, so code written against it runs before the Work layout exists.
//   openWindow(type, props)  open one of the project's cards as a window
//   closeWindow(id)          close a window (no id: the focused one)
//   project                  the project the windows belong to (slug or null)
export const WorkContext = createContext({
  openWindow: () => {},
  closeWindow: () => {},
  project: null,
})
