// The in-page music player.
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
// Driven by `player` events off the GUI bus (App.jsx re-dispatches them as a
// `jarvis-player` window event) and by the operator's own clicks. No
// window.confirm or alert anywhere: iOS standalone suppresses both, which is
// what made other controls silently dead on the phone.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'

const REPORT_EVERY = 10000   // keeps the host's `stale` flag from tripping

// Chrome/Edge only. Safari and iOS have no setSinkId, so the picker is hidden
// there rather than offered and then failing.
const CAN_PICK_OUTPUT = typeof HTMLMediaElement !== 'undefined'
  && 'setSinkId' in HTMLMediaElement.prototype

function clock(s) {
  if (!Number.isFinite(s) || s < 0) return '0:00'
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
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

  return (
    <div className={collapsed ? 'jplayer collapsed' : 'jplayer'}>
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

      <div className="jp-head">
        <div className="jp-meta">
          <div className="jp-title ellipsis" title={track.title}>{track.title}</div>
          <div className="jp-sub ellipsis">
            {[track.artist, track.album].filter(Boolean).join(' — ') || 'library'}
            {left > 0 && <span className="jp-queued">+{left}</span>}
          </div>
        </div>
        <button className="jp-icon" onClick={() => setCollapsed((c) => !c)}
                aria-label={collapsed ? 'expand player' : 'collapse player'}
                title={collapsed ? 'expand' : 'collapse'}>
          {collapsed ? '▴' : '▾'}
        </button>
        <button className="jp-icon" onClick={close}
                aria-label="close player" title="close">✕</button>
      </div>

      {!collapsed && (
        <>
          <div className="jp-seek">
            <span className="jp-time">{clock(position)}</span>
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
            <span className="jp-time">{clock(duration)}</span>
          </div>

          <div className="jp-controls">
            <button className="jp-icon" onClick={() => step(-1)} disabled={index === 0}
                    aria-label="previous track" title="previous">⏮</button>
            <button className="jp-play" onClick={toggle}
                    aria-label={playing ? 'pause' : 'play'}
                    title={playing ? 'pause' : 'play'}>
              {playing ? '❚❚' : '▶'}
            </button>
            <button className="jp-icon" onClick={() => step(1)}
                    disabled={index >= queue.length - 1}
                    aria-label="next track" title="next">⏭</button>

            <span className="grow" />

            <span className="jp-vol">
              <span className="jp-icon flat" aria-hidden="true">🔊</span>
              <input type="range" min="0" max="100" value={volume}
                     aria-label="volume"
                     style={{ '--jp-pct': `${volume}%` }}
                     onChange={(e) => setVolume(Number(e.target.value))} />
            </span>

            {CAN_PICK_OUTPUT && (
              <button className="jp-icon" onClick={() => {
                loadOutputs(); setShowOutputs((o) => !o)
              }} aria-expanded={showOutputs}
                      aria-label="choose audio output" title="audio output">⏻</button>
            )}
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
}
