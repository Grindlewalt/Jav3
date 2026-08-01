// The music player.
//
// Why this exists: TARMAC is a SEPARATE Cloudflare Access application from
// Jarvis, so a browser holding a Jarvis session cannot fetch its /stream/:id —
// the Access cookie is per-application. The host holds the service token and
// re-serves the bytes on our own origin (GET /api/computeruse/tarmac/stream/:id,
// Range forwarded), so this <audio> element can just point at a same-origin URL.
//
// It also fixes the silence. A browser refuses audio.play() in a tab that has
// had no user gesture; TARMAC's PWA hits that constantly because nobody touches
// it, while this tab is the one the operator is already typing in. When play()
// is refused anyway we report that rather than pretending — the host cannot see
// an <audio> element, so every honest claim about playback comes from here.
//
// It can leave the browser. The controls portal into a detached OS window
// (src/detached.js) that floats over the desktop and drags anywhere; the
// <audio> stays here, in the page that started it, because moving a playing
// media element between documents restarts it. So popping out, closing the
// float and popping out again never interrupt a track.
//
// Driven by `player` events off the GUI bus (App.jsx re-dispatches them as a
// `jarvis-player` window event) and by the operator's own clicks. No
// window.confirm or alert anywhere: iOS standalone suppresses both, which is
// what made other controls silently dead on the phone.
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from './api.js'
import { TAB_ID } from './tab.js'
import { CAN_DETACH, openDetached, watchClose } from './detached.js'

const REPORT_EVERY = 10000   // keeps the host's `stale` flag from tripping

// Chrome/Edge only. Safari and iOS have no setSinkId, so the picker is hidden
// there rather than offered and then failing.
const CAN_PICK_OUTPUT = typeof HTMLMediaElement !== 'undefined'
  && 'setSinkId' in HTMLMediaElement.prototype

function clock(s) {
  if (!Number.isFinite(s) || s < 0) return '0:00'
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
}

// Drawn rather than typed: the emoji transport glyphs (⏮ ❚❚ 🔊) render as a
// different font on every platform and were the least native-looking thing on
// screen. One monochrome set, sized by the button.
const STROKE = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }

function Glyph({ name }) {
  const paths = {
    play: <path d="M8.4 5.3a.9.9 0 0 1 1.38-.76l9.9 6.7a.9.9 0 0 1 0 1.52l-9.9 6.7a.9.9 0 0 1-1.38-.76V5.3Z" />,
    pause: <>
      <rect x="7" y="5" width="3.4" height="14" rx="1.5" />
      <rect x="13.6" y="5" width="3.4" height="14" rx="1.5" />
    </>,
    prev: <>
      <rect x="5.4" y="5.6" width="2.1" height="12.8" rx="1" />
      <path d="M19 6.4v11.2a.9.9 0 0 1-1.38.76l-8.8-5.6a.9.9 0 0 1 0-1.52l8.8-5.6A.9.9 0 0 1 19 6.4Z" />
    </>,
    next: <>
      <rect x="16.5" y="5.6" width="2.1" height="12.8" rx="1" />
      <path d="M5 6.4v11.2a.9.9 0 0 0 1.38.76l8.8-5.6a.9.9 0 0 0 0-1.52l-8.8-5.6A.9.9 0 0 0 5 6.4Z" />
    </>,
    volume: <>
      <path d="M11.4 4.5 7 8.2H4.3a1 1 0 0 0-1 1v5.6a1 1 0 0 0 1 1H7l4.4 3.7a.8.8 0 0 0 1.32-.61V5.11a.8.8 0 0 0-1.32-.61Z" />
      <path d="M16.1 9.2a.9.9 0 0 1 1.27.1 4.3 4.3 0 0 1 0 5.4.9.9 0 1 1-1.37-1.16 2.5 2.5 0 0 0 0-3.08.9.9 0 0 1 .1-1.26Z" />
    </>,
    // an AirPlay-shaped glyph, because that is what choosing an output means here
    output: <>
      <rect x="3.2" y="4.4" width="17.6" height="10.6" rx="2.4" {...STROKE} />
      <path d="M12 13.9l4.3 5.2a.55.55 0 0 1-.42.9H8.12a.55.55 0 0 1-.42-.9L12 13.9Z" />
    </>,
    // the macOS picture-in-picture mark: a window with a window in it
    popout: <>
      <rect x="3.2" y="4.9" width="17.6" height="14.2" rx="2.6" {...STROKE} />
      <rect x="11.4" y="10.7" width="7.2" height="5.7" rx="1.4" />
    </>,
    dock: <>
      <rect x="3.2" y="4.9" width="17.6" height="14.2" rx="2.6" {...STROKE} />
      <path d="M12 8.1v6.2m0 0 2.5-2.5M12 14.3l-2.5-2.5" {...STROKE} />
    </>,
    collapse: <path d="m7 10 5 5 5-5" {...STROKE} />,
    expand: <path d="m7 14 5-5 5 5" {...STROKE} />,
    close: <path d="m6.4 6.4 11.2 11.2M17.6 6.4 6.4 17.6" {...STROKE} />,
  }
  return (
    <svg viewBox="0 0 24 24" width="100%" height="100%" fill="currentColor" aria-hidden="true">
      {paths[name]}
    </svg>
  )
}

export default function Player() {
  const audioRef = useRef(null)
  const [queue, setQueue] = useState([])
  const [index, setIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [duration, setDuration] = useState(0)
  const [volume, setVolume] = useState(100)
  const [error, setError] = useState('')
  const [outputs, setOutputs] = useState([])
  const [sinkId, setSinkId] = useState('')
  const [showOutputs, setShowOutputs] = useState(false)
  const [collapsed, setCollapsed] = useState(false)
  const [float, setFloat] = useState(null)   // the detached Window, or null
  // bumped on every play event so "play that again" restarts the current track:
  // keying the load effect on src alone means an identical src is a no-op
  const [playToken, setPlayToken] = useState(0)

  const track = queue[index] || null

  // the reporter reads through a ref so the 10s interval never captures a
  // stale snapshot of the state it is meant to be describing
  const snap = useRef({})
  snap.current = { track, index, queue, playing, position, duration, volume, error }

  const report = useCallback((over = {}) => {
    const s = snap.current
    const body = {
      // whose report this is: another tab still playing from earlier must not
      // be read as this tab having started
      tab: TAB_ID,
      track_id: s.track?.id ?? null,
      title: s.track?.title || '',
      artist: s.track?.artist || '',
      paused: !s.playing,
      position: s.position || 0,
      duration: s.duration || null,
      queue: Math.max(0, s.queue.length - s.index - 1),
      volume: s.volume,
      started: s.playing,
      error: s.error || '',
      ...over,
    }
    api('/api/computeruse/tarmac/player/state', {
      method: 'POST', body: JSON.stringify(body),
    }).catch(() => { /* the host being briefly unreachable must not stop music */ })
  }, [])

  // --- playback ---------------------------------------------------------------

  const attemptPlay = useCallback(() => {
    const el = audioRef.current
    if (!el) return
    const p = el.play()
    if (!p?.then) return
    p.then(() => {
      setError('')
      setPlaying(true)
      report({ started: true, paused: false, error: '' })
    }).catch((e) => {
      // NotAllowedError = the autoplay policy. Anything else is a real load or
      // decode failure; both are worth stating verbatim rather than guessing.
      const msg = e?.name === 'NotAllowedError'
        ? 'the browser blocked autoplay until the page is clicked'
        : (e?.message || String(e))
      setError(msg)
      setPlaying(false)
      report({ started: false, paused: true, error: msg })
    })
  }, [report])

  // a fresh src needs load() before play() in some browsers
  useEffect(() => {
    const el = audioRef.current
    if (!el || !track) return
    el.load()
    attemptPlay()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [track?.src, playToken])

  useEffect(() => {
    const el = audioRef.current
    if (el) el.volume = Math.max(0, Math.min(volume, 100)) / 100
  }, [volume, track?.src])

  useEffect(() => {
    if (!track) return undefined
    const t = setInterval(() => report(), REPORT_EVERY)
    return () => clearInterval(t)
  }, [track, report])

  const step = useCallback((delta) => {
    setIndex((i) => {
      const next = i + delta
      if (next < 0) return 0
      if (next >= snap.current.queue.length) return i
      return next
    })
  }, [])

  const toggle = useCallback(() => {
    const el = audioRef.current
    if (!el) return
    if (el.paused) {
      attemptPlay()
    } else {
      el.pause()
      setPlaying(false)
      report({ paused: true, started: false })
    }
  }, [attemptPlay, report])

  const close = useCallback(() => {
    const el = audioRef.current
    if (el) el.pause()
    setQueue([])
    setIndex(0)
    setPlaying(false)
    setError('')
    report({ track_id: null, paused: true, started: false, queue: 0 })
  }, [report])

  // --- the detached window ------------------------------------------------------

  const popOut = useCallback(async () => {
    if (float) return
    try {
      const { win, dispose } = await openDetached({
        width: 404, height: 178, title: 'Jarvis · music',
      })
      // closing the float only re-docks the controls; the track never stops
      const unwatch = watchClose(win, () => { dispose(); setFloat(null) })
      win.__jarvisDispose = () => { unwatch(); dispose() }
      setCollapsed(false)
      setShowOutputs(false)
      setFloat(win)
    } catch (e) {
      setError(e?.message || 'the pop-out window could not be opened')
    }
  }, [float])

  const dock = useCallback(() => {
    if (!float) return
    float.__jarvisDispose?.()
    setFloat(null)
    float.close()
  }, [float])

  // stopping the music leaves nothing to control, so the window goes too
  useEffect(() => {
    if (!track && float) {
      float.__jarvisDispose?.()
      setFloat(null)
      float.close()
    }
  }, [track, float])

  // it is a window in a task switcher: it should say what is playing
  useEffect(() => {
    if (float && track) float.document.title = `${track.title} · Jarvis`
  }, [float, track])

  useEffect(() => () => {
    // unmounting the player (logout) must not strand a floating window
    if (float) { float.__jarvisDispose?.(); float.close() }
  }, [float])

  // --- audio outputs ----------------------------------------------------------

  const loadOutputs = useCallback(async () => {
    if (!CAN_PICK_OUTPUT || !navigator.mediaDevices?.enumerateDevices) return []
    try {
      const all = await navigator.mediaDevices.enumerateDevices()
      const outs = all.filter((d) => d.kind === 'audiooutput')
      setOutputs(outs)
      return outs
    } catch { return [] }
  }, [])

  // Device LABELS stay blank until the page has been granted a media
  // permission — that is the browser's rule, not ours, and the only way to
  // lift it is to ask for a stream and immediately drop it. Behind a button,
  // because a surprise mic prompt to name a speaker would be alarming.
  const nameOutputs = useCallback(async () => {
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true })
      s.getTracks().forEach((t) => t.stop())
    } catch { /* refused — the picker just stays unlabelled */ }
    loadOutputs()
  }, [loadOutputs])

  const pickOutput = useCallback(async (deviceId) => {
    const el = audioRef.current
    if (!el?.setSinkId) return
    try {
      await el.setSinkId(deviceId)
      setSinkId(deviceId)
      setShowOutputs(false)
    } catch (e) {
      setError(e?.message || 'that output was refused')
    }
  }, [])

  useEffect(() => { loadOutputs() }, [loadOutputs])

  // --- the agent's channel ----------------------------------------------------

  useEffect(() => {
    async function onEvent(e) {
      const ev = e.detail || {}
      switch (ev.action) {
        case 'play': {
          const rows = Array.isArray(ev.queue) ? ev.queue.filter((t) => t?.src) : []
          if (!rows.length) return
          if (typeof ev.volume === 'number') setVolume(ev.volume)
          setError('')
          setQueue(rows)
          setIndex(Math.max(0, Math.min(ev.index || 0, rows.length - 1)))
          setCollapsed(false)
          setPlayToken((t) => t + 1)
          if (ev.output) {
            // resolve the model's words against the outputs THIS browser can
            // actually see — the same rule the desktop client follows, so a
            // string from the model never becomes a device id
            const outs = await loadOutputs()
            const want = String(ev.output).toLowerCase()
            const hit = outs.find((d) => (d.label || '').toLowerCase().includes(want))
            if (hit) pickOutput(hit.deviceId)
          }
          break
        }
        case 'queue_add': {
          // append behind whatever is playing — never interrupts. On an empty
          // player this is just 'play' (there is nothing to be behind).
          const rows = Array.isArray(ev.queue) ? ev.queue.filter((t) => t?.src) : []
          if (!rows.length) return
          const empty = snap.current.queue.length === 0
          setError('')
          setQueue((q) => [...q, ...rows])
          if (empty) {
            setIndex(0)
            setCollapsed(false)
            setPlayToken((t) => t + 1)
          }
          break
        }
        case 'pause': {
          const el = audioRef.current
          if (el && !el.paused) { el.pause(); setPlaying(false); report({ paused: true, started: false }) }
          break
        }
        case 'resume': attemptPlay(); break
        case 'next': step(1); break
        case 'prev': step(-1); break
        case 'volume':
          if (typeof ev.level === 'number') setVolume(Math.max(0, Math.min(ev.level, 100)))
          break
        case 'stop': close(); break
        default: break
      }
    }
    window.addEventListener('jarvis-player', onEvent)
    return () => window.removeEventListener('jarvis-player', onEvent)
  }, [attemptPlay, close, loadOutputs, pickOutput, report, step])

  if (!track) return null

  const pct = duration > 0 ? (position / duration) * 100 : 0
  const left = queue.length - index - 1
  const shut = collapsed && !float

  // One tree, rendered either into the page or into the detached window's body.
  // It is the same React tree either way, so state, the audio element and the
  // 10s reporter carry straight across the move.
  const shell = (
    <div className={`jplayer${float ? ' floating' : ''}${shut ? ' collapsed' : ''}`}>
      <div className="jp-head">
        <div className="jp-meta">
          <div className="jp-title ellipsis" title={track.title}>{track.title}</div>
          <div className="jp-sub ellipsis">
            {[track.artist, track.album].filter(Boolean).join(' — ') || 'library'}
            {left > 0 && <span className="jp-queued">+{left}</span>}
          </div>
        </div>
        {float ? (
          <button className="jp-icon" onClick={dock}
                  aria-label="put the player back in the page" title="put back in the page">
            <Glyph name="dock" />
          </button>
        ) : (
          <>
            {CAN_DETACH && (
              <button className="jp-icon" onClick={popOut}
                      aria-label="float the player on the desktop" title="float on the desktop">
                <Glyph name="popout" />
              </button>
            )}
            <button className="jp-icon" onClick={() => setCollapsed((c) => !c)}
                    aria-label={collapsed ? 'expand player' : 'collapse player'}
                    title={collapsed ? 'expand' : 'collapse'}>
              <Glyph name={collapsed ? 'expand' : 'collapse'} />
            </button>
          </>
        )}
        <button className="jp-icon" onClick={close}
                aria-label="stop and close player" title="stop">
          <Glyph name="close" />
        </button>
      </div>

      {!shut && (
        <>
          <div className="jp-seek">
            <input
              type="range" min="0" max={duration || 0} step="0.1"
              value={Math.min(position, duration || 0)}
              aria-label="seek"
              style={{ '--jp-pct': `${pct}%` }}
              onChange={(e) => {
                const el = audioRef.current
                const v = Number(e.target.value)
                if (el) el.currentTime = v
                setPosition(v)
              }}
            />
            <div className="jp-times">
              <span>{clock(position)}</span>
              <span>{clock(duration)}</span>
            </div>
          </div>

          <div className="jp-controls">
            <span className="jp-side" />

            <div className="jp-transport">
              <button className="jp-icon big" onClick={() => step(-1)} disabled={index === 0}
                      aria-label="previous track" title="previous">
                <Glyph name="prev" />
              </button>
              <button className="jp-play" onClick={toggle}
                      aria-label={playing ? 'pause' : 'play'}
                      title={playing ? 'pause' : 'play'}>
                <Glyph name={playing ? 'pause' : 'play'} />
              </button>
              <button className="jp-icon big" onClick={() => step(1)}
                      disabled={index >= queue.length - 1}
                      aria-label="next track" title="next">
                <Glyph name="next" />
              </button>
            </div>

            <div className="jp-side end">
              <span className="jp-vol">
                <span className="jp-icon flat" aria-hidden="true"><Glyph name="volume" /></span>
                <input type="range" min="0" max="100" value={volume}
                       aria-label="volume"
                       style={{ '--jp-pct': `${volume}%` }}
                       onChange={(e) => setVolume(Number(e.target.value))} />
              </span>

              {CAN_PICK_OUTPUT && (
                <button className="jp-icon" onClick={() => {
                  loadOutputs(); setShowOutputs((o) => !o)
                }} aria-expanded={showOutputs}
                        aria-label="choose audio output" title="audio output">
                  <Glyph name="output" />
                </button>
              )}
            </div>
          </div>

          {showOutputs && (
            <div className="jp-outputs" role="menu">
              {outputs.length === 0 && <div className="jp-note">no outputs found</div>}
              {outputs.map((d) => (
                <button key={d.deviceId} role="menuitem"
                        className={d.deviceId === sinkId ? 'jp-out active' : 'jp-out'}
                        onClick={() => pickOutput(d.deviceId)}>
                  {d.label || 'unnamed output'}
                </button>
              ))}
              {outputs.some((d) => !d.label) && (
                <button className="jp-out jp-name" onClick={nameOutputs}>
                  show device names…
                </button>
              )}
            </div>
          )}

          {error && (
            <div className="jp-error">
              {error}
              {!playing && (
                <button className="jp-retry" onClick={attemptPlay}>play</button>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )

  return (
    <>
      {/* never portalled: a playing media element that changes document
          restarts, so the sound stays put and only the controls travel */}
      <audio
        ref={audioRef}
        src={track.src}
        preload="metadata"
        onTimeUpdate={(e) => setPosition(e.currentTarget.currentTime)}
        onDurationChange={(e) => {
          const d = e.currentTarget.duration
          setDuration(Number.isFinite(d) ? d : (track.duration || 0))
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          if (index < queue.length - 1) step(1)
          else { setPlaying(false); report({ paused: true, started: false }) }
        }}
        onError={() => {
          const msg = 'the track could not be loaded from the music server'
          setError(msg)
          setPlaying(false)
          report({ started: false, paused: true, error: msg })
        }}
      />
      {float ? createPortal(shell, float.document.body) : shell}
    </>
  )
}
