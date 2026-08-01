# Voice desktop mode

Hands-free Jarvis: you talk, he talks back, and interrupting him works the
way interrupting a person does. The full agent is underneath — voice turns
ARE chat turns (same persistence, budget, peak gate, project binding), so
everything a typed conversation can do, a spoken one can too.

## Topology

```
Browser /voice page  ⇄  WS /api/voice/ws  ⇄  Pi backend        ⇄  WS  ⇄  voicebox (main server)
mic capture, playback,   cookie auth          backend/voice.py     bearer   silero VAD
local barge-in VAD                            state machine,       token    faster-whisper small int8
                                              turns, clones                 kokoro bm_lewis
```

- Audio is PCM16 mono: 16 kHz up (mic), 24 kHz down (TTS). Binary WS frames
  carry a 1-byte type header (`0x01` mic, `0x02 | uint32le chunk_id` TTS);
  JSON text frames carry control. Exact shapes: `backend/voice.py` and
  `voicebox/app.py` docstrings.
- The Pi runs zero audio ML and never decodes a sample — it relays opaque
  bytes (~80 KB/s both directions) and orchestrates.
- The browser never talks to the sidecar; one origin, one cookie, and the
  sidecar stays LAN-only.

## The state machine (backend/voice.py)

`LISTENING → THINKING → SPEAKING`, plus `BARGE_PENDING` (playback paused
locally, waiting for the transcript verdict) and `CONFIRM_PEAK` (the peak
pricing question was spoken; the next utterance answers it).

**Latency path:** tokens stream → `voice_text.SpeechChunker` cuts speakable
sentences (min 25 / max 250 chars, abbreviation + decimal guards, code
fences collapse to "(code omitted.)") → each sentence goes to the sidecar as
a TTS request → audio slices stream back and play gaplessly. First audio
lands roughly one sentence after generation starts.

**Voice turns skip the `_enforce_rules` second pass** (`run_turn(...,
rewrite_rules=False)`): that pass rewrites text formatting after the fact,
and rewriting text that was already spoken would silently diverge from what
the operator heard. The spoken stream and the persisted row therefore match.

## Barge-in

The browser's worklet computes RMS per 60 ms mic batch; two hot batches
while audio is playing → `AudioContext.suspend()` **immediately** (position
preserved) and `barge_in {chunk_id, played_ms}` goes up. Nothing else stops
yet: TTS keeps streaming into the browser's buffer while the sidecar
transcribes what the mic heard.

- transcript is empty (cough, chair squeak) → `resume_playback`; the whole
  event was a sub-second hiccup.
- real speech → `tts_cancel` + `stop_playback`, and:
  - turn still streaming prose → it is cancelled with
    `chat.set_interrupt_note`: the assistant row becomes *exactly what was
    spoken* + a cut-off marker, so the next turn's context knows what the
    operator heard and nothing more.
  - turn already finished (audio tail) → the persisted row gains a
    `[voice note: playback was interrupted — heard only up to "…"]` suffix.
  - turn mid-TOOL-WORK → see below; work is never interrupted.

The spoken prefix is computed from played chunk durations (exact, from PCM
byte counts) with the cut snapped to a word boundary.

## Talk-while-working (the twin flow)

If you talk while the agent is running tools, the running turn is left
alone — it is already a detached task — and becomes a **background worker**
on its own conversation. The session rebinds to a **clone**:
`voice.clone_conversation` copies the parent's `compact_summary` (with
`compact_upto` reset) plus all post-checkpoint messages and tool_calls, so
`compaction.assemble` rebuilds the identical model-facing history cheaply.
The clone's transcript opens with a twin note — what the twin is doing, and
how much of the interrupted reply you actually heard — and answers you
immediately. Clones are `kind='chat'` with `parent_conversation_id` set:
both threads are visible in the sidebar.

- Cap: `voice_max_workers` (3). At the cap new speech parks with a canned
  line and drains at the next idle, oldest first.
- A finished worker's result is INSERTed into the talking conversation
  immediately (durable even if the tab is gone) and spoken at the next idle:
  short results whole, long ones as a sentence-bounded digest — the full
  text is in the transcript, no extra model call.

## Configuration

Pi (`~/.config/jarvis/env`):

```
JARVIS_VOICE_ENABLED=true
JARVIS_VOICE_SIDECAR_URL=ws://10.0.0.58:8100/ws
JARVIS_VOICE_SIDECAR_TOKEN=<the sidecar's VOICEBOX_TOKEN>
# JARVIS_VOICE_MAX_WORKERS=3
```

Off by default; the /voice nav link only renders when `/api/config` reports
`voice_enabled`. Sidecar setup: `voicebox/README.md` (Docker or venv;
`VOICEBOX_TOKEN=$(openssl rand -hex 32) docker compose up -d --build`).

## Ops notes

- **Keepalives:** both WS legs ping every 20 s (uvicorn's default browser-
  side, explicit on the sidecar link) — Cloudflare kills idle sockets at
  ~100 s.
- **Sidecar down:** the page shows "voicebox offline"; the link retries with
  backoff (1→30 s); typed chat is unaffected. `GET /api/voice/status` shows
  session/link state.
- **Mic requires a secure context** — the Cloudflare hostname or localhost;
  plain `http://<pi>:8000` will not get `getUserMedia`.
- **Reconnect:** a new /voice tab supersedes the old session (one operator,
  one voice channel). Worker watchers survive the tab: results still land in
  the transcript; announcements resume on the next session that binds the
  same conversation.
- **Bus under load:** the token firehose can shed bus events (drop-oldest);
  the chunker's source of truth heals at `final` by speaking the unspoken
  suffix. The voice consumer never blocks on socket I/O.

## Tests

- `tests/test_voice_text.py` — chunker, sanitizer, spoken-prefix math.
- `tests/test_voice_seams.py` — interrupt-note seam, `rewrite_rules` flag.
- `tests/test_voice_session.py` — the state machine with fake transports:
  happy path, all three barge-in verdicts.
- `tests/test_voice_clone.py` — clone SQL vs. compaction, cap, delivery.
- `voicebox/tests/test_pipeline.py` — TTS→VAD→STT round trip (x86 only,
  self-skips without models).

All pure-logic — no model calls, per repo policy.
