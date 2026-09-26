// Every live feed, on ONE connection per browser (not per tab).
//
// Over plain http a browser allows six connections per host, shared by all its
// tabs. Each Jav3 tab used to hold three or four feeds open for its whole life
// (the GUI channel, security alerts, agent notices, the Network page's egress
// feed), so two tabs used every connection and each ordinary fetch after that
// queued forever — the Work page's "+ window" just never opened.
//
// Now one tab per browser is the leader (a Web Lock held for the tab's life).
// It alone opens GET /api/events, which carries every topic, and rebroadcasts
// each event on a BroadcastChannel. Every tab — the leader too — hands events
// to its own subscribers. When the leader closes or reloads, the lock passes to
// another tab, which opens the stream again (a short gap, like a reconnect).
//
// Tab-addressed GUI events. The host addresses music to ONE machine by tab id
// (backend/gui.py). With a shared connection the host cannot tell tabs apart by
// socket, so the leader tells it which tabs are alive behind its connection
// (PUT /api/gui/conn/{conn}/tabs, from the hello/heartbeat/bye messages tabs
// send here), the host stamps an addressed event with its target (`to`), and
// every tab drops the ones that are not its own.
//
// Opening events. Each feed used to send `stream_open` on connect. A tab (or a
// subscriber) that joins after the stream opened still gets one: each tab keeps
// the last opening event per topic and replays it to a late subscriber, and the
// leader answers a new tab's hello with the opening events it has.
//
// No Web Locks or BroadcastChannel: this tab opens its own /api/events (one
// connection per tab instead of three or four — the old behaviour, less so).

import { TAB_ID, tabName } from './tab.js'

export const TOPICS = ['gui', 'security', 'notices', 'egress']

// the old per-feed URLs, so api.js subscribeSse callers need not change
export const URL_TOPIC = {
  '/api/gui/stream': 'gui',
  '/api/security/stream': 'security',
  '/api/agents/notices/stream': 'notices',
  '/api/egress/stream': 'egress',
}

const CHANNEL = 'jav3-events'
const HEARTBEAT_MS = 20000
// generous: a hidden tab's timers can be throttled to once a minute, and a
// background tab dropped from the list could not be sent music
const STALE_MS = 180000

const subs = new Map()      // topic -> Set(fn)
const opens = {}            // topic -> last opening event seen here
let started = false
let bc = null

function deliver(topic, event, to) {
  if (to && to !== TAB_ID) return               // addressed to another tab
  if (event && event.type === 'stream_open') {
    // a tab's own GUI stream said which tab it was; keep that shape
    if (topic === 'gui') event = { type: 'stream_open', channel: 'gui', tab: TAB_ID }
    opens[topic] = event
  }
  for (const fn of subs.get(topic) || []) {
    try { fn(event) } catch (e) { console.error(e) }
  }
}

function post(msg) {
  try { bc?.postMessage(msg) } catch { /* closed channel */ }
}

// --- leader -----------------------------------------------------------------

function lead() {
  const live = new Map()    // tab id -> { name, seen }
  const leaderOpens = {}    // topic -> raw opening event (from the host)
  let conn = ''
  let es = null
  let synced = ''

  const syncTabs = () => {
    if (!conn) return
    live.set(TAB_ID, { name: tabName(), seen: Date.now() })
    const tabs = [...live].map(([id, t]) => ({ id, name: t.name }))
    const key = conn + JSON.stringify(tabs)
    if (key === synced) return
    synced = key
    fetch(`/api/gui/conn/${encodeURIComponent(conn)}/tabs`, {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tabs }),
    }).then((r) => { if (!r.ok) synced = '' }).catch(() => { synced = '' })
  }

  const connect = () => {
    es = new EventSource('/api/events?topics=' + TOPICS.join(','))
    es.onmessage = (m) => {
      let f
      try { f = JSON.parse(m.data) } catch { return }
      const { topic, event, to } = f || {}
      if (!topic) return
      if (event?.type === 'stream_open') {
        leaderOpens[topic] = event
        if (topic === 'gui') { conn = event.conn || ''; syncTabs() }
      }
      post({ kind: 'event', topic, event, to })
      deliver(topic, event, to)
    }
    es.onerror = () => {
      // CONNECTING: the browser retries by itself. CLOSED (a 401 before login
      // finished, a 5xx): it never will, so do it here.
      if (es.readyState === EventSource.CLOSED) {
        conn = ''
        setTimeout(connect, 3000)
      }
    }
  }

  if (bc) {
    bc.onmessage = (m) => {
      const d = m.data || {}
      if (d.kind === 'hello' && d.tab) {
        const known = live.get(d.tab)
        live.set(d.tab, { name: d.name || '', seen: Date.now() })
        if (!known) post({ kind: 'open', to: d.tab, opens: leaderOpens })
        if (!known || known.name !== d.name) syncTabs()
      } else if (d.kind === 'bye' && d.tab) {
        if (live.delete(d.tab)) syncTabs()
      }
    }
    post({ kind: 'who' })       // a new leader knows nobody yet
  }
  setInterval(() => {
    const now = Date.now()
    for (const [id, t] of live) {
      if (id !== TAB_ID && now - t.seen > STALE_MS) live.delete(id)
    }
    syncTabs()
  }, HEARTBEAT_MS)
  addEventListener('pagehide', () => { try { es?.close() } catch { /* */ } })
  connect()
}

// --- every tab ----------------------------------------------------------------

function follow() {
  const hello = () => post({ kind: 'hello', tab: TAB_ID, name: tabName() })
  bc.onmessage = (m) => {
    const d = m.data || {}
    if (d.kind === 'event') deliver(d.topic, d.event, d.to)
    else if (d.kind === 'who') hello()
    else if (d.kind === 'open' && d.to === TAB_ID) {
      for (const [topic, ev] of Object.entries(d.opens || {})) deliver(topic, ev)
    }
  }
  hello()
  setInterval(hello, HEARTBEAT_MS)
  addEventListener('pagehide', () => post({ kind: 'bye', tab: TAB_ID }))
}

function start() {
  if (started) return
  started = true
  const shared = typeof BroadcastChannel !== 'undefined'
    && typeof navigator !== 'undefined' && navigator.locks?.request
  if (!shared) { lead(); return }
  bc = new BroadcastChannel(CHANNEL)
  follow()
  navigator.locks.request(CHANNEL, () => {
    // this tab is the leader now. Stop following (the leader delivers its own
    // events directly) but keep answering: hello/bye go to the leader handler.
    lead()
    return new Promise(() => {})        // held until the tab goes away
  }).catch(() => {})
}

// Subscribe to one topic's events. Returns the unsubscribe.
export function subscribe(topic, fn) {
  if (!subs.has(topic)) subs.set(topic, new Set())
  subs.get(topic).add(fn)
  start()
  // a late subscriber still hears that its feed is open, as it did on connect
  if (opens[topic]) {
    const ev = opens[topic]
    queueMicrotask(() => { if (subs.get(topic)?.has(fn)) fn(ev) })
  }
  return () => { subs.get(topic)?.delete(fn) }
}
