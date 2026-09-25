import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { notifyError } from '../notify.js'

// What is waiting on the operator in this chat's scope, for the transcript to
// show inline: network hosts the egress proxy is holding, git commit/remote
// requests, and plan items that stopped to ask. Approvals are keyed by scope
// (a project slug, or a project-less chat's `chat-<id>` store), not by
// conversation — the tables have no conversation column — so a chat shows its
// scope's queue, placed by time into the turn that most likely raised it.
//
// Refreshed on open, every few seconds while a turn runs, and whenever the
// caller's `poke` changes (the shell passes the sidebar's count for this
// scope, which it polls anyway — so a request raised by a turn in another tab
// still shows up here).

const LIVE_POLL_MS = 4000

// SQLite's "YYYY-MM-DD HH:MM:SS" and the plan file's ISO "…T…Z" compared as
// one shape (both are UTC)
export const tsKey = (s) => (s ? String(s).replace('T', ' ').slice(0, 19) : '')

function planQuestions(plan) {
  return (plan?.items || []).filter((it) => it.status === 'blocked'
    && !(it.last_error || '').startsWith('dependency '))
}

export function useApprovals({ scope, isProject, busy, poke, onChanged }) {
  const [items, setItems] = useState([])
  const [acting, setActing] = useState(null)       // key of the row being decided
  const seq = useRef(0)

  const load = useCallback(async () => {
    const mine = ++seq.current
    if (!scope) { setItems([]); return }
    const enc = encodeURIComponent(scope)
    const [egress, git, plan] = await Promise.all([
      api(`/api/egress/pending?project=${enc}`).then((r) => r.pending).catch(() => []),
      isProject
        ? api(`/api/projects/${enc}/git/requests`).then((r) => r.requests).catch(() => [])
        : [],
      isProject
        ? api(`/api/projects/${enc}/plan`).then((r) => r.plan).catch(() => null)
        : null,
    ])
    if (mine !== seq.current) return
    setItems([
      ...egress.map((e) => ({ ...e, key: `e${e.id}`, kind: 'egress', ts: tsKey(e.first_seen) })),
      // the row's own `kind` (commit | remote) is kept as git_kind
      ...git.filter((g) => g.status === 'pending')
        .map((g) => ({ ...g, key: `g${g.id}`, kind: 'git', git_kind: g.kind,
                       ts: tsKey(g.created_at) })),
      // a plan question has no timestamp of its own: it belongs to "now"
      ...planQuestions(plan).map((it) => ({ key: `p${it.id}`, kind: 'plan', ts: '',
                                            item: it })),
    ])
  }, [scope, isProject])

  useEffect(() => { load() }, [load, poke])
  useEffect(() => {
    if (!busy) return undefined
    const t = setInterval(load, LIVE_POLL_MS)
    return () => { clearInterval(t); load() }      // one last look as the turn ends
  }, [busy, load])

  const decide = useCallback(async (row, action) => {
    const enc = encodeURIComponent(scope)
    const req = {
      egress: () => api(`/api/egress/pending/${row.id}/${action}`, { method: 'POST' }),
      git: () => api(`/api/projects/${enc}/git/requests/${row.id}/${action}`,
                     { method: 'POST' }),
      plan: () => api(`/api/projects/${enc}/plan/items/${encodeURIComponent(row.item.id)}`,
                      { method: 'PATCH', body: JSON.stringify({ status: action }) }),
    }[row.kind]
    setActing(row.key)
    try { await req() } catch (err) { notifyError(err) }
    setActing(null)
    await load()
    onChanged?.()
  }, [scope, load, onChanged])

  return { items, acting, decide }
}

// Where each approval goes in the transcript: before the first reply that was
// written after it was raised (i.e. inside the turn that raised it). Anything
// newer than every finished reply — including everything raised by the turn
// still streaming — goes at the end.
export function placeApprovals(messages, items) {
  const before = new Map()
  const tail = []
  for (const a of items) {
    const i = a.ts ? messages.findIndex((m) => m.role === 'assistant' && m.created_at
                                              && tsKey(m.created_at) >= a.ts) : -1
    if (i === -1) tail.push(a)
    else before.set(i, [...(before.get(i) || []), a])
  }
  return { before, tail }
}
