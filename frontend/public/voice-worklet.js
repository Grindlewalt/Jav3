// Voice-mode capture worklet: float32 @ 16 kHz in (the AudioContext is opened
// at 16 kHz, so the browser has already resampled), 60 ms PCM16 batches out,
// each with its RMS so the main thread can run the barge-in VAD without
// touching the samples again. Lives in public/ so Vite serves it verbatim for
// audioWorklet.addModule('/voice-worklet.js').
const BATCH = 960; // 60 ms @ 16 kHz

class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super()
    this.buf = new Float32Array(BATCH)
    this.n = 0
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (!ch) return true
    let i = 0
    while (i < ch.length) {
      const take = Math.min(BATCH - this.n, ch.length - i)
      this.buf.set(ch.subarray(i, i + take), this.n)
      this.n += take
      i += take
      if (this.n === BATCH) {
        const out = new Int16Array(BATCH)
        let sum = 0
        for (let j = 0; j < BATCH; j++) {
          const s = Math.max(-1, Math.min(1, this.buf[j]))
          sum += s * s
          out[j] = s * 0x7fff
        }
        this.port.postMessage(
          { pcm: out.buffer, rms: Math.sqrt(sum / BATCH) }, [out.buffer])
        this.n = 0
      }
    }
    return true
  }
}

registerProcessor('voice-capture', VoiceCapture)
