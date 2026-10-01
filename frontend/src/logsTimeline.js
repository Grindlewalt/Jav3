// The Logs transcript's timeline, as the API sends it (/api/logs/conversations/{id}).
//
// Three kinds of item, in the order things happened: `message` (the operator's
// or the assistant's), `tool` (a call with its args and result), and
// `narration` (the text the agent wrote between its calls: its own words, not a
// reply and not a tool result). Only messages and narration are prose, and only
// narration is labelled as the agent thinking aloud.

// The one line above the timeline: how many items, and how many of them are the
// agent's between-calls text (left out when a chat from before that was kept
// has none)
export function timelineHeader(items = []) {
  const notes = items.filter((i) => i.kind === 'narration').length
  const parts = [`${items.length} item${items.length === 1 ? '' : 's'}`]
  if (notes) parts.push(`${notes} agent note${notes === 1 ? '' : 's'}`)
  parts.push('in order')
  return parts.join(' · ')
}

// The role label of a prose item: what the reader sees as its head
export function proseLabel(item) {
  return item.kind === 'narration' ? "agent's own text" : item.role
}
