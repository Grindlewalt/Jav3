// The voice page's audio engine: mic capture (worklet, 16 kHz PCM16 batches),
// TTS playback (24 kHz, gaplessly scheduled AudioBufferSources), and the
// local barge-in VAD.
//
// The barge-in contract: the INSTANT the operator speaks over playback we
// suspend the playback context locally (suspend/resume preserves position
// exactly), report the position, and let the server's transcript verdict
// decide — resume_playback (false alarm: ctx.resume(), nothing lost) or
// stop_playback (real: tear the queue down). Audio keeps arriving while the
// verdict is pending; it just buffers into the scheduled queue.

const CAPTURE_RATE = 16000
const PLAY_RATE = 24000

// RMS gate: trip after N consecutive hot 60 ms batches. The threshold rises
// while audio is playing — echoCancellation strips most of our own TTS from
// the mic, the higher bar covers what leaks through.
const VAD_BASE = 0.02
const VAD_PLAYING_MULT = 3
const VAD_TRIP_BATCHES = 2

// Double clap: two sharp attacks (loud batch rising straight out of quiet —
// speech ramps, a clap doesn't) 150-800ms apart. A single clap can't trip the
// barge-in VAD (it needs two consecutive hot batches; a clap is one).
const CLAP_MIN = 0.22
const CLAP_QUIET = 0.07
const CLAP_GAP_MIN = 150
const CLAP_GAP_MAX = 800
const CLAP_REFRACTORY = 1500

export class VoiceAudio {
  constructor({ onMicFrame, onBargeIn, onChunkPlayed, onLevel, onDoubleClap }) {
    this.onMicFrame = onMicFrame
    this.onBargeIn = onBargeIn
    this.onChunkPlayed = onChunkPlayed
    this.onLevel = onLevel || (() => {})
    this.onDoubleClap = onDoubleClap || (() => {})
    this.muted = false
    this._hot = 0
    this._prevRms = 0
    this._lastClap = 0
    this._clapFired = 0
    this._suspended = false     // local barge pause in effect
    this._segments = []         // {chunkId, source, start, end, stopped}
    this._ttsEnded = new Set()  // chunk ids the server finished sending
    this._nextTime = 0
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    this.capCtx = new AudioContext({ sampleRate: CAPTURE_RATE })
    await this.capCtx.audioWorklet.addModule('/voice-worklet.js')
    const src = this.capCtx.createMediaStreamSource(this.stream)
    this.node = new AudioWorkletNode(this.capCtx, 'voice-capture')
    src.connect(this.node)
    this.node.port.onmessage = ({ data }) => {
      this.onLevel(data.rms)
      this._vad(data.rms)
      this._clap(data.rms)
      if (!this.muted) this.onMicFrame(data.pcm)
    }
    this.playCtx = new AudioContext({ sampleRate: PLAY_RATE })
  }

  stop() {
    try { this.stream?.getTracks().forEach((t) => t.stop()) } catch { /* gone */ }
    try { this.capCtx?.close() } catch { /* gone */ }
    try { this.playCtx?.close() } catch { /* gone */ }
  }

  get playing() {
    if (this._suspended) return true    // paused mid-chunk still counts
    const t = this.playCtx ? this.playCtx.currentTime : 0
    return this._segments.some((s) => !s.stopped && s.end > t)
  }

  // ---- playback --------------------------------------------------------------

  enqueue(chunkId, pcmBuffer) {
    if (!this.playCtx || pcmBuffer.byteLength < 2) return
    const i16 = new Int16Array(pcmBuffer)
    const f32 = new Float32Array(i16.length)
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000
    const buf = this.playCtx.createBuffer(1, f32.length, PLAY_RATE)
    buf.getChannelData(0).set(f32)
    const source = this.playCtx.createBufferSource()
    source.buffer = buf
    source.connect(this.playCtx.destination)
    const start = Math.max(this.playCtx.currentTime + 0.06, this._nextTime)
    const seg = { chunkId, source, start, end: start + buf.duration, stopped: false }
    source.onended = () => this._segEnded(seg)
    source.start(start)
    this._nextTime = seg.end
    this._segments.push(seg)
  }

  chunkComplete(chunkId) {
    this._ttsEnded.add(chunkId)
    this._sweepPlayed()
  }

  _segEnded(seg) {
    seg.done = true
    if (!seg.stopped) this._sweepPlayed()
  }

  _sweepPlayed() {
    // a chunk is "played" when the server said it sent everything (tts_end)
    // and every scheduled segment of it has finished
    for (const id of [...this._ttsEnded]) {
      const segs = this._segments.filter((s) => s.chunkId === id)
      if (segs.length && segs.every((s) => s.done && !s.stopped)) {
        this._ttsEnded.delete(id)
        this._segments = this._segments.filter((s) => s.chunkId !== id)
        this.onChunkPlayed(id)
      }
    }
  }

  // Two quick rising notes — the "I'm listening" cue after a wake word.
  chime() {
    if (!this.playCtx) return
    const t0 = this.playCtx.currentTime + 0.02
    for (const [freq, at] of [[740, 0], [1109, 0.09]]) {
      const osc = this.playCtx.createOscillator()
      const gain = this.playCtx.createGain()
      osc.frequency.value = freq
      gain.gain.setValueAtTime(0.0001, t0 + at)
      gain.gain.exponentialRampToValueAtTime(0.12, t0 + at + 0.015)
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.12)
      osc.connect(gain).connect(this.playCtx.destination)
      osc.start(t0 + at)
      osc.stop(t0 + at + 0.14)
    }
  }

  // Instant local pause; position is preserved by the context clock.
  bargePause() {
    if (this._suspended || !this.playCtx) return null
    const pos = this.position()
    this._suspended = true
    this.playCtx.suspend()
    return pos
  }

  resume() {
    if (this._suspended) {
      this._suspended = false
      this.playCtx.resume()
    }
  }

  // Real barge-in: drop everything scheduled, ready for the next reply.
  stopAll() {
    for (const s of this._segments) {
      s.stopped = true
      try { s.source.stop() } catch { /* already ended */ }
    }
    this._segments = []
    this._ttsEnded.clear()
    this._nextTime = 0
    if (this._suspended) {
      this._suspended = false
      this.playCtx.resume()
    }
  }

  // Where playback stands: the chunk under the playhead and how many ms of
  // that chunk have been heard (summed across its segments).
  position() {
    const t = this.playCtx.currentTime
    let cur = null
    for (const s of this._segments) {
      if (s.start <= t && t < s.end) { cur = s; break }
      if (s.start > t) break
    }
    if (!cur) {
      // between segments: report the last fully-played chunk boundary
      const before = this._segments.filter((s) => s.end <= t)
      if (!before.length) return { chunk_id: 0, played_ms: 0 }
      const last = before[before.length - 1]
      const ms = before.filter((s) => s.chunkId === last.chunkId)
        .reduce((a, s) => a + (s.end - s.start), 0) * 1000
      return { chunk_id: last.chunkId, played_ms: Math.round(ms) }
    }
    const prior = this._segments
      .filter((s) => s.chunkId === cur.chunkId && s.end <= t)
      .reduce((a, s) => a + (s.end - s.start), 0)
    return {
      chunk_id: cur.chunkId,
      played_ms: Math.round((prior + (t - cur.start)) * 1000),
    }
  }

  // ---- barge-in VAD ------------------------------------------------------------

  _clap(rms) {
    const sharp = rms >= CLAP_MIN && this._prevRms <= CLAP_QUIET
    this._prevRms = rms
    if (this.muted || !sharp) return
    const now = performance.now()
    if (now - this._clapFired < CLAP_REFRACTORY) return
    const gap = now - this._lastClap
    if (this._lastClap && gap >= CLAP_GAP_MIN && gap <= CLAP_GAP_MAX) {
      this._lastClap = 0
      this._clapFired = now
      this.onDoubleClap()
    } else {
      this._lastClap = now
    }
  }

  _vad(rms) {
    const gate = this.playing ? VAD_BASE * VAD_PLAYING_MULT : VAD_BASE
    if (this.muted || rms < gate) {
      this._hot = 0
      return
    }
    this._hot += 1
    if (this._hot === VAD_TRIP_BATCHES && this.playing && !this._suspended) {
      const pos = this.bargePause()
      if (pos) this.onBargeIn(pos)
    }
  }
}
