import { createContext, useContext } from 'react'

// What the Work page offers the things it hosts (the chat composer's slash
// commands, a card that wants to open another card):
//
//   openWindow(type, props?)  open a window of a card type (the keys of
//                             WINDOW_TYPES in work/windows.jsx: 'plan', 'git',
//                             'terminal', 'journal', …) beside the focused one,
//                             or focus an open one of that type. `props` is
//                             merged into the new card's state. Returns the
//                             window id, or null when it could not open (no
//                             project on this chat, unknown type).
//   closeWindow(id)           close a window by id ('chat' is not closable).
//   project                   the slug the windows belong to (the chat's
//                             current project), or null.
//
// Outside Work (a page that renders Chat on its own) the default below is
// used: nothing opens, and callers can tell from `available`.
export const WorkContext = createContext({
  available: false,
  project: null,
  openWindow: () => null,
  closeWindow: () => {},
})

export const useWork = () => useContext(WorkContext)
