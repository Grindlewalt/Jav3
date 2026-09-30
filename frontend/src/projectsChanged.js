// The Projects sheet creates, renames, loads and deletes projects, but the
// sidebar list (Chat.jsx), the chip menu (Work.jsx) and the /shell sidebar each
// hold their own copy of GET /api/projects. One window event, like
// `jarvis-files-changed`, tells them to refetch.
const EVENT = 'jarvis-projects-changed'

export function projectsChanged() {
  window.dispatchEvent(new Event(EVENT))
}

/** Subscribe; returns the unsubscribe function (a useEffect cleanup). */
export function onProjectsChanged(fn) {
  window.addEventListener(EVENT, fn)
  return () => window.removeEventListener(EVENT, fn)
}
