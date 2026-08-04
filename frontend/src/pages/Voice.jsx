import { useEffect, useRef, useState } from 'react'
import { TAB_ID } from '../tab.js'
import { VoiceAudio } from '../voiceAudio.js'

// Desktop voice mode: fully hands-free. The page is a status surface — the
// conversation happens out loud. Mic PCM streams up the WS, TTS PCM streams
// down, and the transcript renders as it happens so you can glance over.
//
// Barge-in is local-first: voiceAudio.js suspends playback the instant the
// VAD trips, then the server's transcript verdict either resumes (noise) or
// tears the queue down (real speech).

const STATE_LABEL = {
  connecting: 'connecting…',
  listening: 'listening',
  thinking: 'thinking…',
  speaking: 'speaking',
  barge_pending: 'you were saying?',
  confirm_peak: 'confirm?',
  confirm_escalate: 'send it up?',
  asleep: 'say “hey Jarvis”',
  offline: 'voicebox offline',
  mic_denied: 'microphone blocked',
}

export default function Voice() {
  const [state, setState] = useState('connecting')
  const [working, setWorking] = useState(false)
  const [tier, setTier] = useState('local')     // which brain answered
  const [feed, setFeed] = useState([])          // {role, text, id}
  const [workers, setWorkers] = useState([])
  const [muted, setMuted] = useState(false)
  const [level, setLevel] = useState(0)
  const [err, setErr] = useState('')
  const [cid, setCid] = useState(null)
  const wsRef = useRef(null)
  const audioRef = useRef(null)
  const feedRef = useRef(null)
  const seq = useRef(0)

  const push = (role, text, tier) =>
    setFeed((f) => [...f.slice(-199), { role, text, tier, id: seq.current++ }])

  useEffect(() => {
    let dead = false
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/api/voice/ws`)
    ws.binaryType = 'arraybuffer'
    wsRef.current = ws

    const audio = new VoiceAudio({
      onMicFrame: (pcm) => {
        if (ws.readyState !== 1) return
        const framed = new Uint8Array(1 + pcm.byteLength)
        framed[0] = 0x01
        framed.set(new Uint8Array(pcm), 1)
        ws.send(framed)
      },
      onBargeIn: (pos) => {
        if (ws.readyState === 1)
          ws.send(JSON.stringify({ type: 'barge_in', ...pos }))
      },
      onChunkPlayed: (id) => {
        if (ws.readyState === 1)
          ws.send(JSON.stringify({ type: 'chunk_played', chunk_id: id }))
      },
      onLevel: (rms) => setLevel((old) => old * 0.6 + rms * 0.4),
      onDoubleClap: () => {
        if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'double_clap' }))
      },
      onClap: () => push('sys', '(👏)'),   // each registered clap, for tuning
    })
    audioRef.current = audio

    ws.onopen = async () => {
      ws.send(JSON.stringify({ type: 'hello', tab: TAB_ID }))
      try {
        await audio.start()
      } catch (e) {
        if (!dead) { setState('mic_denied'); setErr(String(e?.message || e)) }
      }
    }
    ws.onclose = (ev) => {
      if (dead) return
      setState(ev.code === 4404 ? 'offline' : 'connecting')
      if (ev.code === 4404) setErr('voice mode is disabled (JARVIS_VOICE_ENABLED)')
    }
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        const view = new Uint8Array(ev.data)
        if (view[0] === 0x02 && view.length >= 5) {
          const id = new DataView(ev.data).getUint32(1, true)
          audio.enqueue(id, ev.data.slice(5))
        }
        return
      }
      const msg = JSON.parse(ev.data)
      switch (msg.type) {
        case 'ready': setState('listening'); setErr(''); break
        case 'state':
          setState(msg.state)
          setWorking(!!msg.turn_working)
          if (msg.tier) setTier(msg.tier)
          if (msg.conversation_id) setCid(msg.conversation_id)
          break
        case 'conversation': setCid(msg.id); break
        case 'transcript': push('user', msg.text); break
        case 'assistant_text':
          push(msg.system ? 'sys' : 'assistant', msg.text, msg.tier)
          break
        case 'tts_end': audio.chunkComplete(msg.chunk_id); break
        case 'stop_playback': audio.stopAll(); break
        case 'resume_playback': audio.resume(); break
        case 'queued': push('sys', `(queued: “${msg.text}”)`); break
        case 'clap': push('sys', `(👏👏 ${msg.result || msg.title})`); break
        case 'wake': audio.chime(); break
        case 'shutdown': push('sys', '(shutting down — say “hey Jarvis” after reload)'); break
        case 'tool_activity':
          if (msg.phase === 'call') push('tool', msg.name)
          break
        case 'workers': setWorkers(msg.list || []); break
        case 'worker_done': push('sys', `(background task finished)`); break
        case 'error': setErr(msg.message || 'error'); break
        default: break
      }
    }
    return () => {
      dead = true
      try { ws.close() } catch { /* already closed */ }
      audio.stop()
    }
  }, [])

  useEffect(() => {
    feedRef.current?.scrollTo(0, feedRef.current.scrollHeight)
  }, [feed])

  const toggleMute = () => {
    const next = !muted
    setMuted(next)
    if (audioRef.current) audioRef.current.muted = next
    const ws = wsRef.current
    if (ws?.readyState === 1) ws.send(JSON.stringify({ type: 'mute', on: next }))
  }

  const orbClass = `voice-orb ${state}${working ? ' working' : ''}`
  const glow = Math.min(1, level * 12)

  return (
    <div className="voice-page">
      <div className="voice-stage">
        <div className={orbClass}
             style={{ '--mic-glow': state === 'listening' ? glow : 0 }}>
          <div className="voice-orb-core" />
        </div>
        <div className="voice-state">{STATE_LABEL[state] || state}
          {tier === 'smart' && <span className="voice-tier-tag">smart model</span>}
          {working && <span className="voice-working-tag">background work running</span>}
        </div>
        {err && <div className="voice-err">{err}</div>}
        <div className="voice-controls">
          <button className={muted ? '' : 'ghost'} onClick={toggleMute}>
            {muted ? 'Unmute mic' : 'Mute mic'}
          </button>
          {cid && <a className="ghost-link" href={`/?c=${cid}`}>open transcript</a>}
        </div>
      </div>

      {workers.length > 0 && (
        <div className="voice-workers">
          {workers.map((w) => (
            <div key={w.conversation_id} className="voice-worker">
              <span className="run-dot running" />
              <span className="voice-worker-task">{w.task}</span>
              <span className="voice-worker-status">{w.status}</span>
            </div>
          ))}
        </div>
      )}

      <div className="voice-feed" ref={feedRef}>
        {feed.length === 0 && (
          <div className="voice-hint">
            Just start talking. Interrupt any time — he’ll stop.
          </div>
        )}
        {feed.map((m) => (
          <div key={m.id} className={`voice-line ${m.role}`}>
            {m.role === 'tool' ? `⚙ ${m.text}` : m.text}
            {m.role === 'assistant' && m.tier === 'smart' &&
              <span className="voice-line-tier">smart</span>}
          </div>
        ))}
      </div>
    </div>
  )
}
