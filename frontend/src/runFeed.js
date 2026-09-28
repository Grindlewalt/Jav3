import { api } from './api.js'
import { subscribe } from './events.js'
import { snapshotEvents } from './runSnapshot.js'

// Follow one agent job's tree by its head conversation id, WITHOUT a
// connection of its own. Each expanded JobTree (and the plan panel) used to
// hold GET /api/runs/{cid}/stream open; a browser allows six connections per
// host across every tab, so a few open trees froze the app. Now:
//
//   snapshot  GET /api/runs/{cid}/tree?depth=full (an ordinary fetch), turned
//             into the same events the old stream opened with (runSnapshot.js)
//   live      the `runs` topic on the shared per-browser stream (events.js,
//             backend/events_api.py): every job's events stamped with job_id,
//             filtered here to this one
//
// Live events that arrive while the snapshot is loading are held and replayed
// after it, so nothing between the two is lost (node_spawned merges, so an
// overlap is harmless). When the shared stream reopens (a new leader tab, a
// reconnect) the snapshot is taken again: events in the gap are gone.
//
// onEvent(ev) gets the old stream's event shapes. onError(err) when the run
// cannot be read (not a job, deleted). Returns the unsubscribe.
export function followRun(cid, onEvent, onError) {
  let jobId = null
  let loading = true          // the first snapshot starts below
  let closed = false
  let held = []

  const sync = () => {
    loading = true
    api(`/api/runs/${cid}/tree?depth=full`).then((r) => {
      if (closed) return
      const nodes = (r && r.nodes) || []
      jobId = nodes.find((n) => n.id === cid)?.job_id || null
      if (!jobId) throw Object.assign(new Error('not a job'), { status: 404 })
      for (const ev of snapshotEvents(cid, nodes)) onEvent(ev)
      const later = held
      held = []
      for (const ev of later) if (ev.job_id === jobId) onEvent(ev)
    }).catch((e) => { if (!closed) onError?.(e) })
      .finally(() => { loading = false })
  }

  const unsub = subscribe('runs', (ev) => {
    if (closed || !ev) return
    if (ev.type === 'stream_open') {
      // the replayed opening event lands while the first snapshot loads
      if (!loading) sync()
      return
    }
    if (loading) held.push(ev)
    else if (jobId && ev.job_id === jobId) onEvent(ev)
  })
  sync()
  return () => { closed = true; held = []; unsub() }
}
