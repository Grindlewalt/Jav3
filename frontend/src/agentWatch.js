// Which agent runs the operator is actually looking at right now.
//
// An interactive run is detached server-side, so it finishes whether or not
// anyone is attached, and it announces itself on /api/agents/notices/stream.
// Notices uses this to decide whether that announcement is news: a run whose
// panel is on screen in a visible tab needs no toast — the operator is
// watching the tokens arrive. Everything else (navigated away, panel closed,
// tab in the background) does.
//
// A module-level Set rather than context/state on purpose: the panel that
// registers and the notice layer that reads are on opposite sides of the tree,
// and this never needs to trigger a render.

const watched = new Set()

/** Mark a run as on-screen. Returns the un-watch function (use it as the
 *  effect cleanup so an unmount always releases). */
export function watchRun(conversationId) {
  if (conversationId == null) return () => {}
  watched.add(conversationId)
  return () => watched.delete(conversationId)
}

/** True only if this run is mounted somewhere AND the tab is actually
 *  visible — switching tabs counts as clicking off. */
export function isWatched(conversationId) {
  return watched.has(conversationId) && !document.hidden
}
