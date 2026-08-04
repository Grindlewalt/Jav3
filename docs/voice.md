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

## The local fast tier

With `JARVIS_VOICE_LOCAL_MODEL` set (`gemma-4:12b` since 2026-08-04), voice turns run on
the operator's own ollama box by default — conversation, media control,
quick questions: no API cost, no peak gate (the gate only prices DeepSeek
hours), first token in ~120 ms over the LAN. Three ways a turn reaches
DeepSeek instead:

1. **The model bounces it**: the local prompt (`voice_text.LOCAL_PROMPT`)
   tells it to reply with a single `[ESCALATE] <reason>` line for anything
   heavy. The orchestrator never speaks that line (opening tokens are held
   until the reply is provably not an escalation), scrubs it from the
   transcript, and asks out loud — "…Want me to send it up?". Yes → the same
   utterance reruns on DeepSeek against clean history. No → dropped.
2. **The operator names it**: "smart model" / "deepseek" / "big model"
   anywhere in an utterance routes it straight up, no ask.
3. **The switch at the top of the page**: Local / Flash, persisted in
   `session_state` under `voice_force_tier`, so it survives a reload. Local is
   the default and still escalates; Flash sends every turn to DeepSeek with no
   escalation question — the setting you flip *before* asking for real work out
   loud. Spoken keywords still force a single turn up either way.
4. **The tier is off** (`voice_local_model` empty): everything runs on
   DeepSeek as before.

Local turns get a slim context (`voice.LOCAL_CONTEXT_EXCLUDE` — soul, user and
the active project survive; the behaviour doctrine, env, heavyweight indexes
and full standing-memory notes don't) and a tool set shipped without its Notes
bodies: a 4B in an 8k window can't carry the full sandwich, and prefilling it
costs seconds. See the latency budget below — this is not a nicety, an
oversized prompt silently truncates the tool definitions. The operator-rules
tail is never droppable, so the hard rules still bind local turns.

They also get two things the smart tier does not:

**Past turns' tool work, replayed** (`compaction.assemble(tool_trace=...)`).
The model-facing history is `role`/`content` only, so a turn that actually
played a song reads back as "the operator asked, the assistant said 'Playing it
now.'" — a worked example of talking instead of acting. DeepSeek shrugs that
off; a 4B copies it, and stops calling tools at all. Measured against the live
tier with the production prompt, on "play some Zach Bryan":

| history | tool calls |
|---|---|
| none | 6/6 |
| 2 prose-only exchanges | 0/6 |
| 5 prose-only exchanges | 0/6 |
| 5 exchanges replayed with their tool turns | 12/12 |

`tool_calls.message_id` (added 2026-08-04) is the link that makes the replay
possible; second-resolution timestamps can't do it, because a voice turn fits
inside one second. Results are truncated to
`voice_local_tool_trace_chars` — the point is to show that acting happens
through tool calls, not to re-feed a page. Rows written before the column
carry no trace and replay as prose.

**The whole music library, in the prompt** (`voice_text.library_block`). Thirty
titles is a few hundred tokens, cheaper than the `music_search` round trip it
replaces, and it stops the model inventing plausible songs. Two things there are
load-bearing and both measure *worse than nothing* if done the obvious way: the
rules under the list (see `LIBRARY_RULES`), and the fact that each track's id
**trails** its title rather than leading it — `[26] Mockingbird` had gemma-4:12b
returning a neighbour's number 4/4, i.e. confidently playing the wrong song.

## The latency budget (rebuilt 2026-08-03, re-measured 2026-08-04)

Target: **speech end → first audio ≤ 880 ms**. Measured median **691 ms**:

| leg | cost | where |
|---|---|---|
| whisper (small, int8_float16) | 73 ms | voicebox, cuda:0 |
| LLM to first spoken clause | ~308 ms | llama.cpp :11436, **cuda:1** |
| TTS of that clause | ~310 ms | architect-tts :8123, cuda:1 |

**Card placement is worth more than model size.** The LLM used to share cuda:0
with whisper, and that contention — not any llama.cpp floor — cost ~150 ms of
TTFT. The note that once claimed a "hard ~190 ms per-request floor" was wrong:
the same 4B on an idle card does 76–79 ms. Moving to cuda:1 paid for a model
twice the size and still came out 131 ms ahead:

| config | TTFT | first clause | chain |
|---|---|---|---|
| 4B on cuda:0 (until 2026-08-04) | 264 ms | 439 ms | 822 ms |
| 9B on cuda:1 | 107 ms | 344 ms | 727 ms |
| **gemma-4:12b on cuda:1 (current)** | 405 ms | 663 ms | **1046 ms** |

The 12B is deliberately over the 880 ms target. It was chosen on capability:
30/50 → 44/50 on the voice gauntlet, and the 9B's failures were almost all
fabrication ("Playing Seven Nation Army, sir." with no tool call) on the two
commonest ways music gets asked for. 1046 ms is inside the 1-1.5 s band
research calls acceptable. Its `--reasoning-budget 0` is load-bearing — see
`scripts/llama-voice.service`.

Three things hold that number up, and all three are easy to undo by accident:

1. **The prompt must fit the window.** Voice system prompt + tool schemas used
   to be ~8.8k tokens against an 8k window. Overflow truncated the prompt,
   killed the KV prefix cache (a full 4.0 s re-prefill *every turn*) and ate
   part of the tool block — which is why the model would emit tool calls as
   prose JSON. `LOCAL_CONTEXT_EXCLUDE` now also drops `behavior` and `env.md`,
   and the local toolset ships with `notes_max=0`. Budget: ~4.5k tokens.
2. **Thinking must be off.** qwen3.5 defaults to a reasoning block — 69–197
   tokens of silence before the first word. The switch is llama-server's
   `--chat-template-kwargs '{"enable_thinking":false}'`; `--reasoning-budget 0`
   alone does **not** work.
3. **The first spoken clause must be short.** The synth runs ~6× realtime, so
   first-audio ≈ (spoken seconds)/6 + ~180 ms — driven by *spoken length*, not
   character count. `VOICE_PROMPT` asks for a six-word opener and
   `FIRST_CUT_MAX` caps it at 40 chars. The one case still over budget is a
   short-but-slow answer like "There are 5,280 feet in a mile." (31 chars, 3 s
   of speech, 550 ms of TTS).

`VoiceSession.start` also warms the prompt prefix the moment the tab opens:
resident weights are only half of "no waiting" — cold, the ~4.5k-token prefix
is a 2.3 s prefill on the operator's first word.

Remaining known floor: llama.cpp costs a flat **~190 ms per request** on this
box regardless of prompt size, flags, batch size or CUDA graphs (CPU-only is
140 ms). That is the next real win if voice ever needs to be faster.

**Serving the local tier (the main server, needs operator sudo):**
- `scripts/llama-voice.service` — llama.cpp on :11436, gemma-4:12b, cuda:1.
  **Not actually installed**: the live tier is a nohup'd process, so it dies
  on reboot and nothing restarts it. Installing the unit needs sudo on main.
- `scripts/architect-tts.service` — the architect voice on :8123, cuda:1.
- **Disable `ollama-voice`** once these are in: it pins a 5.4 GB model on the
  same 8 GB card llama-server needs.
- Ollama's own qwen3.5 GGUF will not load in llama.cpp (`rope.dimension_sections`
  is length 3, llama.cpp wants 4) — use the `unsloth/Qwen3.5-4B-GGUF` build.

## Configuration

Pi (`~/.config/jarvis/env`):

```
JARVIS_VOICE_ENABLED=true
JARVIS_VOICE_SIDECAR_URL=ws://10.0.0.58:8100/ws
JARVIS_VOICE_SIDECAR_TOKEN=<the sidecar's VOICEBOX_TOKEN>
JARVIS_VOICE_LOCAL_MODEL=gemma-4:12b
JARVIS_VOICE_LOCAL_BASE_URL=http://10.0.0.58:11436/v1
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
