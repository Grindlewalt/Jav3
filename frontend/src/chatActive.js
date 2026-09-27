// The chat list's Active group, as data (ChatGroups.jsx polls; this folds).
// Pure so node can test it: frontend/src/__tests__/chatActive.test.mjs.

// id -> { running, agents, needs } for every chat with something in flight.
// `running`: the chat's own turn (or its orchestrator plan) is live; `agents`:
// how many nodes under it are running; `needs`: why it waits on the operator,
// or null. Folded from GET /api/chat/running and /api/chat/agents?scope=active.
export function activeFrom(running, nodes) {
  const out = new Map()
  const get = (id) => {
    if (!out.has(id)) out.set(id, { running: false, agents: 0, needs: null })
    return out.get(id)
  }
  for (const id of running || []) get(id).running = true
  // nodes come root first, then its subtree depth-first
  let root = null
  const ids = new Set()
  for (const n of nodes || []) {
    if (n.parent_id == null || !ids.has(n.parent_id)) root = n.id
    ids.add(n.id)
    if (n.status !== 'running' && n.status !== 'needs_you') continue
    const a = get(root)
    if (n.id === root) { if (n.status === 'running') a.running = true }
    else if (n.status === 'running') a.agents += 1
    if (n.status === 'needs_you' && !a.needs) a.needs = n.needs || 'needs you'
  }
  for (const [id, a] of out) if (!a.running && !a.agents && !a.needs) out.delete(id)
  return out
}
