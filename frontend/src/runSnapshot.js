// A job tree's snapshot (GET /api/runs/{cid}/tree?depth=full rows) as the
// events the old per-run stream opened with (backend/runs_api.py run_stream).
// Pure, so node can test it (__tests__/runSnapshot.test.mjs); runFeed.js does
// the fetching and the live half.

// "[research] Topic" -> "Topic", as the old stream titled nodes
const title = (s) => {
  const t = s || ''
  const i = t.indexOf('] ')
  return i < 0 ? t : t.slice(i + 2)
}

// the tree endpoint's rows -> node_spawned (+ node_done) per node, then
// job_final when the head has its rollup
export function snapshotEvents(cid, nodes) {
  const ids = new Set(nodes.map((n) => n.id))
  const parent = new Map(nodes.map((n) => [n.id, n.parent_conversation_id]))
  const depth = (id) => {
    let d = 0
    let p = parent.get(id)
    const seen = new Set([id])
    while (p != null && ids.has(p) && !seen.has(p)) { seen.add(p); d += 1; p = parent.get(p) }
    return d
  }
  const out = []
  for (const n of nodes) {
    // the head's parent is the chat that launched the job: outside the tree
    const p = n.parent_conversation_id
    out.push({ type: 'node_spawned', node_id: n.id, parent_id: ids.has(p) ? p : null,
               kind: n.kind, title: title(n.summary), depth: depth(n.id) })
    if (n.rollup != null) out.push({ type: 'node_done', node_id: n.id, rollup: n.rollup })
  }
  const head = nodes.find((n) => n.id === cid)
  if (head && head.rollup != null) {
    out.push({ type: 'job_final', job_id: head.job_id, root_id: cid })
  }
  return out
}
