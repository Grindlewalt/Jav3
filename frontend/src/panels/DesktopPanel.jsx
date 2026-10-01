import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useAsk } from '../ask.jsx'
import { subscribe } from '../events.js'
import { notifyError } from '../notify.js'
import { listBoxes } from '../boxes/api/vms.js'
import EmptyState from '../components/EmptyState.jsx'
import {
  SANDBOX_WARNING, agentActive, boxOptionLabel, candidateBoxes, canTake, displayWsUrl, eventTouches,
  heldSeconds, mmss, newViewerId, panelMode, pickBox, startBlocked, startLabel, statusLabel, stopTurns,
  takeLine, takesOver, viewOf, viewersText,
} from '../desktop/logic.js'

// A Work window: the box's screen, live. It starts as a view: the server drops
// every key, mouse and clipboard message from a window that does not hold
// control (backend/vm/display_api.py), and noVNC is told viewOnly as well.
//
// The operator takes the desktop with a click (or a key) on the screen, which
// pauses the agent, and gives it back with [Hand back] or by closing the window
// (the server waits 10 s for it to come back). Control and "the agent just
// acted" ride the shared stream (topic vm-boxes), never an EventSource of their own.
//
// The window never starts a box by itself. Opening it only reads; [Start
// desktop] is the one thing that boots a box or the screen. The noVNC library
// is loaded when a screen is first shown, so the main bundle does not carry it.

const stamp = (v) => (v ? { ...v, at: Date.now() } : null)

export default function DesktopPanel({ slug, state, setState }) {
  const ask = useAsk()
  const chosen = state?.box || null
  const [rows, setRows] = useState(null)         // GET /api/vm/boxes rows, or null
  const [st, setSt] = useState(null)             // GET .../display for the box
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)        // the start call is in flight
  const [conn, setConn] = useState('idle')       // noVNC: idle | connecting | watching | closed | lost
  const [gen, setGen] = useState(0)              // bump to (re)connect
  const [viewerId] = useState(() => newViewerId())   // this window's noVNC session, for control
  const [ctl, setCtl] = useState(null)           // who holds the desktop, stamped with `at`
  const [agent, setAgent] = useState(null)       // when the agent last acted, and its turns
  const [now, setNow] = useState(() => Date.now())
  const [taking, setTaking] = useState(false)
  const screenRef = useRef(null)
  const rfbRef = useRef(null)

  const box = pickBox(rows, slug, chosen)
  const boxId = box?.id || null
  const boxPath = boxId ? `/api/vm/boxes/${encodeURIComponent(boxId)}` : null

  // the box list, and what the chosen box's desktop is doing
  const refresh = useCallback(async () => {
    try {
      const l = await listBoxes()
      setRows(l.boxes)
      const b = pickBox(l.boxes, slug, chosen)
      if (!b) { setSt(null); setErr(null); return }
      const s = await api(`/api/vm/boxes/${encodeURIComponent(b.id)}/display`)
      setSt(s)
      setCtl(stamp(s.control))
      setAgent(stamp(s.agent))
      setErr(null)
    } catch (e) {
      setErr(e.detail || e.message || 'could not read the desktop')
    }
  }, [slug, chosen])

  useEffect(() => { refresh() }, [refresh])

  // the shared stream: the box coming or going, the screen starting, viewers, and
  // (P3) who holds control and whether the agent just acted
  useEffect(() => {
    if (!boxId) return undefined
    let t = null
    const off = subscribe('vm-boxes', (ev) => {
      if (!eventTouches(ev, boxId)) return
      if (ev.type === 'display' && (ev.control || ev.agent)) {
        if (ev.control) setCtl(stamp(ev.control))
        if (ev.agent) setAgent(stamp(ev.agent))
        return
      }
      clearTimeout(t)
      t = setTimeout(refresh, 400)
    })
    return () => { off(); clearTimeout(t) }
  }, [boxId, refresh])

  const active = agentActive(agent, now)
  const view = viewOf(ctl, viewerId, active)         // you | other | agent | watching
  const held = heldSeconds(ctl, now)

  // the clock runs only while something on screen counts: the hold, or the agent's quiet spell
  useEffect(() => {
    if (view !== 'you' && view !== 'agent') return undefined
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [view])

  // an already-running screen is just watched: no start, no boot
  const live = st?.supported && st.session === 'running'
  useEffect(() => {
    if (live && conn === 'idle') setGen((g) => g + 1)
    if (!live && conn !== 'idle') setConn('idle')
  }, [live, conn])

  // the noVNC connection
  useEffect(() => {
    if (!gen || !live || !boxId) return undefined
    let rfb = null
    let dead = false
    setConn('connecting')
    ;(async () => {
      let RFB
      try {
        RFB = (await import('@novnc/novnc')).default
      } catch (e) {
        if (!dead) { setErr('the viewer library did not load: ' + (e.message || e)); setConn('lost') }
        return
      }
      if (dead || !screenRef.current) return
      rfb = new RFB(screenRef.current, displayWsUrl(window.location, boxId, viewerId),
        { wsProtocols: ['binary'] })
      rfbRef.current = rfb
      rfb.viewOnly = true            // until the operator takes control (below); the server drops input too
      rfb.scaleViewport = true       // 1280x800 scaled to the window
      rfb.clipViewport = false
      rfb.resizeSession = false
      rfb.background = 'transparent'
      rfb.addEventListener('connect', () => setConn('watching'))
      rfb.addEventListener('disconnect', (e) => {
        if (dead) return
        setConn(e.detail?.clean ? 'closed' : 'lost')
        refresh()                    // the screen may have stopped, or the box
      })
      rfb.addEventListener('securityfailure', () => { if (!dead) setConn('lost') })
    })()
    return () => {
      dead = true
      rfbRef.current = null
      try { rfb?.disconnect() } catch { /* already closed */ }
    }
  }, [gen, live, boxId, viewerId, refresh])

  // input goes to the box only while THIS window holds control (the server checks too)
  useEffect(() => {
    const rfb = rfbRef.current
    if (!rfb) return
    rfb.viewOnly = view !== 'you'
    if (view === 'you') rfb.focus()
  }, [view, conn])

  const start = async () => {
    setBusy(true); setErr(null)
    try {
      setSt(await api(`/api/vm/boxes/${encodeURIComponent(boxId)}/display`,
        { method: 'POST', body: '{}' }))
    } catch (e) {
      setErr(e.detail || e.message || 'the desktop did not start')
    } finally {
      setBusy(false)
    }
  }

  const control = async (body) => {
    const r = await api(`${boxPath}/display/control`, { method: 'POST', body: JSON.stringify(body) })
    setCtl(stamp(r.control))
    setNow(Date.now())
  }

  // the first click (or key) on the screen is consumed: it takes control, it is not sent to the box
  const take = async () => {
    if (!canTake(view, conn) || taking) return
    setTaking(true)
    try {
      await control({ holder: 'operator', viewer: viewerId })
    } catch (e) {
      notifyError(e)
    } finally {
      setTaking(false)
    }
  }

  const handBack = async () => {
    try { await control({ holder: 'agent' }) } catch (e) { notifyError(e) }
  }

  // [Stop]: the agent's turn in this box, through the ordinary stop endpoint
  const turns = stopTurns(view, active, agent)
  const stopAgent = async () => {
    if (!await ask.confirm('Stop the agent’s turn in this box?',
      { confirmLabel: 'Stop', danger: true })) return
    await Promise.all(turns.map((id) =>
      api(`/api/chat/${id}/stop`, { method: 'POST' }).catch((e) => notifyError(e))))
  }

  const choose = (id) => { setState({ box: id || null }); setConn('idle'); setCtl(null) }
  const options = candidateBoxes(rows)
  const mode = panelMode(st, conn, err)
  const blocked = startBlocked(st)
  const connected = mode === 'screen' && conn === 'watching'

  return (
    <div className="pane-col desktop-pane">
      <div className="row desktop-head">
        <span className={`dim small grow desk-status ${conn} ${connected ? view : ''}`}>
          {connected ? statusLabel(view, held)
            : mode === 'screen' ? 'connecting…'
              : mode === 'ended' ? 'disconnected'
                : mode === 'idle' ? 'desktop off'
                  : boxId ? 'desktop' : 'no box'}
          {boxId && <span className="dim"> · {boxId}</span>}
          {st && viewersText(st.viewers) && <span className="dim"> · {viewersText(st.viewers)}</span>}
        </span>
        {options.length > 1 && (
          <select value={boxId || ''} onChange={(e) => choose(e.target.value)}
                  aria-label="box to watch">
            {options.map((b) => <option key={b.id} value={b.id}>{boxOptionLabel(b)}</option>)}
          </select>
        )}
        {mode === 'ended' && <button className="ghost" onClick={() => { setConn('idle'); refresh() }}>reconnect</button>}
      </div>

      <div className="desk-warning" role="note">{SANDBOX_WARNING}</div>

      {connected && view !== 'you' && (
        <div className={`desk-bar ${view}`}>
          <span className="grow">{takeLine(view)}</span>
          {turns.length > 0 && <button className="ghost danger" onClick={stopAgent}>Stop</button>}
        </div>
      )}

      {mode === 'screen' && (
        <div className="desk-stage">
          <div className="desk-screen" ref={screenRef}
               aria-label={view === 'you' ? 'the box’s screen, you have control' : 'the box’s screen, view only'} />
          {canTake(view, conn) && (
            <div className="desk-takeover" role="button" tabIndex={0}
                 aria-label="take control of the desktop: the agent is paused while you have it"
                 title="click to take over"
                 onClick={take}
                 onKeyDown={(e) => { if (takesOver(e.key)) { e.preventDefault(); take() } }} />
          )}
        </div>
      )}

      {view === 'you' && (mode === 'screen' || mode === 'ended') && (
        <div className="desk-bar you" role="status">
          <span className="grow"><strong>YOU have control</strong>: agent paused {mmss(held)}</span>
          <button onClick={handBack}>Hand back</button>
        </div>
      )}

      {mode === 'loading' && !boxId && rows && (
        <EmptyState pad hint="A project gets a box when its first turn runs. Give it the desktop image under Runs in (VMs, Boxes), then come back.">
          {slug ? `${slug} has no box yet` : 'no project box to watch'}
        </EmptyState>
      )}
      {mode === 'loading' && boxId && <EmptyState pad>checking {boxId}…</EmptyState>}
      {mode === 'loading' && !rows && <EmptyState pad>checking boxes…</EmptyState>}

      {mode === 'unsupported' && (
        <EmptyState pad hint="The desktop is a KVM image layer: there is no Docker desktop.">
          {st.reason}
        </EmptyState>
      )}

      {mode === 'idle' && (
        <div className="desk-idle">
          <div className="desk-idle-title">The desktop is off</div>
          <p className="dim small">
            A live screen of {boxId}, 1280×800. It starts only when you press the button
            {st?.state === 'stopped' ? ' (this also boots the box)' : ''}, and stops by itself
            five minutes after the last window closes. The agent drives it; click the screen to
            take over, and hand it back when you are done.
          </p>
          <button onClick={start} disabled={busy || !!blocked} title={blocked || undefined}>
            {busy ? 'Starting…' : startLabel(st)}
          </button>
          {blocked && <p className="warn">{blocked}</p>}
        </div>
      )}

      {mode === 'ended' && (
        <EmptyState pad hint={view === 'you'
          ? 'You still hold the desktop for a few seconds: reconnect keeps it, otherwise the agent gets it back.'
          : 'The screen stopped, or the box did. reconnect looks again.'}>
          the connection to the screen ended
        </EmptyState>
      )}

      {mode === 'error' && (
        <div className="desk-idle">
          <p className="warn">{err}</p>
          <button className="ghost" onClick={refresh}>try again</button>
        </div>
      )}
    </div>
  )
}
