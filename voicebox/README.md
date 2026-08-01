# voicebox

The Jarvis voice inference sidecar: silero VAD + faster-whisper STT +
Kokoro TTS behind one authenticated WebSocket. Runs on an x86 box (the main
server) — the Pi that hosts Jarvis only relays audio bytes to and from it.

CPU-only, no torch: whisper runs on CTranslate2 (int8), Kokoro and silero on
onnxruntime.

## Run with Docker (recommended)

```sh
cd voicebox
VOICEBOX_TOKEN=$(openssl rand -hex 32) docker compose up -d --build
docker compose logs -f     # first start downloads ~600 MB of models
curl -s localhost:8100/health
```

Keep the token: the Jarvis backend needs it as `JARVIS_VOICE_SIDECAR_TOKEN`.
Models persist in the `voicebox-models` volume; restarts don't re-download.

## Run in a venv

```sh
cd voicebox
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python download_models.py          # into ./models
VOICEBOX_TOKEN=$(openssl rand -hex 32) \
  .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8100
```

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `VOICEBOX_TOKEN` | *(unset — refuses all connections)* | bearer token the Jarvis backend presents |
| `VOICEBOX_MODELS` | `./models` (`/models` in Docker) | model directory |
| `VOICEBOX_WHISPER` | `small` | faster-whisper size (`tiny`/`base`/`small`/`medium`) |
| `VOICEBOX_LANG` | `en` | STT language pin (autodetect costs latency) |
| `VOICEBOX_VOICE` | `bm_lewis` | Kokoro voice |
| `VOICEBOX_SPEED` | `1.0` | TTS speed |

## Protocol

One WebSocket, `/ws`, header `Authorization: Bearer <token>`. A second
connection supersedes the first (reconnect friendliness). Mixed frames:

- **binary up** `0x01 | PCM16 mono @ 16 kHz` — mic audio, any chunk size
- **binary down** `0x02 | uint32le tts_id | PCM16 mono @ 24 kHz` — synthesized
  audio in ~0.5 s slices
- **text up** `{"type":"tts","id":N,"text":"…"}` ·
  `{"type":"tts_cancel"}` (drop queue + abort current between slices) ·
  `{"type":"reset"}` (drop partial VAD state)
- **text down** `ready` · `speech_start` · `speech_end` ·
  `transcript {text, dur_ms}` (empty text = noise/false trigger) ·
  `tts_start {id}` · `tts_done {id, dur_ms}` · `error {message}`

## Smoke test

```sh
.venv/bin/python demo_client.py --token $VOICEBOX_TOKEN \
    --wav speech16k.wav --say "It works." --out reply.wav
```

`tests/test_pipeline.py` round-trips TTS → VAD → STT and self-skips when the
models aren't present (so the main repo's test run on the Pi never touches it).
