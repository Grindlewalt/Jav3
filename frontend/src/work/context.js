import { createContext, useContext } from 'react'

// What the Work page offers the things it hosts (the chat composer's slash
// commands, a card that wants to open another card):
//
//   openWindow(type, props?)  open a window of a card type (the keys of
//                             WINDOW_TYPES in work/types.js: 'plan', 'git',
//                             'terminal', 'journal', …) beside the focused one,
//                             or focus an open one of that type. `props` is
//                             merged into the card's state (a `slug` key is
//                             ignored: windows follow the chat's project).
//                             Other spellings work too: todo, taskboard,
//                             grants (work/types.js ALIASES). Returns the
//                             window id, or null when it could not open (no
//                             project on this chat, unknown type).
//   closeWindow(id?)          close a window by id; with no id, the focused
//                             window (nothing when none is). The chat is not
//                             a window and cannot be closed.
//   focused                   the focused window's id, or null.
//   project                   the slug the windows belong to (the chat's
//                             current project), or null.
//
// Outside Work (a page that renders Chat on its own) the default below is
// used: nothing opens, and callers can tell from `available`.
export const WorkContext = createContext({
  available: false,
  project: null,
  focused: null,
  openWindow: () => null,
  closeWindow: () => {},
})

export const useWork = () => useContext(WorkContext)
