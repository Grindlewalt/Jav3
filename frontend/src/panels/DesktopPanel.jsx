import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { subscribe } from '../events.js'
import { listBoxes } from '../boxes/api/vms.js'
import EmptyState from '../components/EmptyState.jsx'
import {
  SANDBOX_WARNING, WATCH_LABEL, boxOptionLabel, candidateBoxes, displayWsUrl, eventTouches, panelMode,
  pickBox, startBlocked, startLabel, viewersText,
} from '../desktop/logic.js'

// A Work window: the box's screen, live. P1 is watch only: the server drops
// every key, mouse and clipboard message this page could send
// (backend/vm/display_api.py), and noVNC is told viewOnly as well.
//
// The window never starts a box by itself. Opening it only reads; [Start
// desktop] is the one thing that boots a box or the screen. The noVNC library
// is loaded when a screen is first shown, so the main bundle does not carry it.

export default function DesktopPanel({ slug, state, setState }) {
  const chosen = state?.box || null
  const [rows, setRows] = useState(null)         // GET /api/vm/boxes rows, or null
  const [st, setSt] = useState(null)             // GET .../display for the box
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState(false)        // the start call is in flight
  const [conn, setConn] = useState('idle')       // noVNC: idle | connecting | watching | closed | lost
  const [gen, setGen] = useState(0)              // bump to (re)connect
  const screenRef = useRef(null)

  const box = pickBox(rows, slug, chosen)
  const boxId = box?.id || null

  // the box list, and what the chosen box's desktop is doing
  const refresh = useCallback(async () => {
    try {
      const l = await listBoxes()
      setRows(l.boxes)
      const b = pickBox(l.boxes, slug, chosen)
      if (!b) { setSt(null); setErr(null); return }
      setSt(await api(`/api/vm/boxes/${encodeURIComponent(b.id)}/display`))
      setErr(null)
    } catch (e) {
      setErr(e.detail || e.message || 'could not read the desktop')
    }
  }, [slug, chosen])

  useEffect(() => { refresh() }, [refresh])

  // the shared stream: the box coming or going, the screen starting, viewers
  useEffect(() => {
    if (!boxId) return undefined
    let t = null
    const off = subscribe('vm-boxes', (ev) => {
      if (!eventTouches(ev, boxId)) return
      clearTimeout(t)
      t = setTimeout(refresh, 400)
    })
    return () => { off(); clearTimeout(t) }
  }, [boxId, refresh])

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
      rfb = new RFB(screenRef.current, displayWsUrl(window.location, boxId),
        { wsProtocols: ['binary'] })
      rfb.viewOnly = true            // the server drops input too; this is the polite half
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
      try { rfb?.disconnect() } catch { /* already closed */ }
    }
  }, [gen, live, boxId, refresh])

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

  const choose = (id) => { setState({ box: id || null }); setConn('idle') }
  const options = candidateBoxes(rows)
  const mode = panelMode(st, conn, err)
  const blocked = startBlocked(st)

  return (
    <div className="pane-col desktop-pane">
      <div className="row desktop-head">
        <span className={`dim small grow desk-status ${conn}`}>
          {mode === 'screen' && conn === 'watching' ? WATCH_LABEL
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

      {mode === 'screen' && (
        <div className="desk-screen" ref={screenRef}
             aria-label="the box’s screen, view only" />
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
            five minutes after the last window closes. You watch; the agent drives.
          </p>
          <button onClick={start} disabled={busy || !!blocked} title={blocked || undefined}>
            {busy ? 'Starting…' : startLabel(st)}
          </button>
          {blocked && <p className="warn">{blocked}</p>}
        </div>
      )}

      {mode === 'ended' && (
        <EmptyState pad hint="The screen stopped, or the box did. reconnect looks again.">
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
