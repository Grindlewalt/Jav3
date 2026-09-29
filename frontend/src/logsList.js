// The Transcripts list's refresh rule (WEB-22).
//
// The list polls every 15 s, and a new conversation used to be inserted at the
// top, sliding every row down under the pointer: a click aimed at one row
// opened the one that had moved into its place. Rows the operator can already
// see now stay where they are; what changed in them is updated in place, ones
// the server no longer has are dropped, and conversations that appeared since
// are counted (`newCount`) behind a "show" pill instead of inserted.
//
// current: the list on screen (null before the first load); incoming: the
// server's newest list. Returns {list, newCount}.
export function mergeConvos(current, incoming) {
  const fresh = Array.isArray(incoming) ? incoming : []
  if (!current) return { list: fresh, newCount: 0 }
  const byId = new Map(fresh.map((c) => [c.id, c]))
  const list = current.filter((c) => byId.has(c.id)).map((c) => byId.get(c.id))
  const seen = new Set(list.map((c) => c.id))
  return { list, newCount: fresh.filter((c) => !seen.has(c.id)).length }
}
