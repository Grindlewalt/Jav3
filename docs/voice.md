# Voice desktop mode

Hands-free Jarvis: you talk, he talks back, and interrupting him works the
way interrupting a person does. The full agent is underneath — voice turns
ARE chat turns (same persistence, budget, peak gate, project binding), so
everything a typed conversation can do, a spoken one can too.

## Topology

```
Browser /voice page  ⇄  WS /api/voice/ws  ⇄  Pi backend        ⇄  WS  ⇄  voicebox (main server)
mic capture, playback,   cookie auth          backend/voice.py     bearer   silero VAD (adaptive)
local barge-in VAD                            state machine,       token    whisper large-v3-turbo
                                              turns, clones                 architect (chatterbox)
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

The verdict is **evidence, not word count** (2026-08-09). It used to be "the
transcript came back non-empty", which is how someone playing guitar in the
same room could stop Jarvis mid-sentence: the RMS gate trips on any loud
sound, whisper is handed music and returns fluent invented words, and
non-empty text read as the operator talking. The sidecar now reports what it
actually knows and `VoiceSession._is_interrupt` weighs all of it:

| signal | source | rejects |
|---|---|---|
| `speech_ratio` | silero, fraction of frames scored as speech | music, thuds, room tone |
| `phantom` | `stt.is_phantom` — a named list of whisper's stock fabrications | "Thanks for watching", `[Music]`, `you you you you` |
| `confident` | whisper's `no_speech_prob` + `avg_logprob` | decodes it was guessing at |

Measured separation: operator speech scores 0.39–0.97 on `speech_ratio` *even
talking over a guitar*, guitar alone 0.00–0.07, strummed chords 0.00–0.15. The
threshold sits at 0.30, in the middle of that gap — 100% of speech kept, 100%
of music rejected. A phantom is dropped in **every** state, not just under
playback: acting on "Thank you for watching" starts a turn nobody asked for.

- transcript is empty, or the evidence says it was the room (cough, chair
  squeak, a guitar) → `resume_playback`; the whole event was a sub-second
  hiccup.
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
# quiet hours for the double clap — "hey Jarvis" is NOT gated (see below)
# JARVIS_VOICE_CLAP_CURFEW=true
# JARVIS_VOICE_CLAP_CURFEW_START=22:30
# JARVIS_VOICE_CLAP_CURFEW_END=07:30
```

Sidecar (`VOICEBOX_*`, on the main server):

```
VOICEBOX_WHISPER=large-v3-turbo   # rollback: small (see the STT note below)
VOICEBOX_DEVICE=cuda
VOICEBOX_COMPUTE=int8_float16     # 1201 MiB on cuda:0
VOICEBOX_HANG_MS=300              # snappy hangover, for a whole sentence
VOICEBOX_HANG_LONG_MS=500         # ...and for a lead-in that may continue
VOICEBOX_STANDALONE_MS=1200       # below this, use the long hangover
# VOICEBOX_TEMP_FALLBACK=          # auto by model class; force with 0/1
```

### Why the double clap has quiet hours and the wake word does not

The clap is the one trigger with no words in it and no confirmation step — a
dropped book at 3 a.m. starts music. Between 22:30 and 07:30 the gesture is
ignored (`voice.in_clap_curfew`, window wraps midnight). "Hey Jarvis" is
deliberately *not* gated: it takes a deliberate spoken sentence to fire, so it
cannot go off by accident, and Jarvis should still work at night.

### STT: why `large-v3-turbo` and adaptive endpointing (2026-08-09)

Clean speech does not show the difference between whisper sizes — every model
gets a close-mic'd clip right. The operator's condition is a room mic at a
distance with a guitar going. Measured over 8 utterances × 3 noise
realizations (reverb ≈0.45 s RT60 at 0.55 gain, guitar mixed at the stated
SNR):

| config | clean | room | gtr@10 | gtr@5 | gtr@0 | latency | VRAM |
|---|---|---|---|---|---|---|---|
| `small` / beam1 temp0 (old) | 10.8% | 19.4% | 17.2% | 22.6% | 26.5% | 79 ms | 777 MiB |
| `small` / hardened + hotwords | 4.9% | 13.1% | 26.1% | 32.9% | 42.9% | 113 ms | 785 MiB |
| `large-v3-turbo` / hardened | 4.9% | 8.7% | 11.3% | 12.6% | 13.9% | 216 ms | 1201 MiB |

**The temperature fallback is only safe on a strong model** — look at the
middle row. Hardening `small` helps in a quiet room and makes it 2–3× worse
under a guitar, because a weak model re-sampling at temperature 0.4+ on noisy
audio invents fluent text instead of returning the mangled-but-honest version.
So the fallback is enabled by model class (`stt._wants_fallback`), not
globally.

**The bigger bug was endpointing, not the model.** At a flat 300 ms hangover
the VAD split ordinary sentences at their internal pauses: "Jarvis, put
Mockingbird on" ended at the comma, whisper saw one second of audio and
returned "Jarvis, Kickstart", and the operator got an answer to something they
never said. The hangover is now adaptive — a brief utterance must hold its
silence longer than a whole sentence, because the pauses that cause splits
follow a short lead-in ("Jarvis,", "So,", "Actually,") while a real end-of-turn
comes after a full sentence:

| hangover | sentences split | median endpoint latency |
|---|---|---|
| flat 300 ms | 1/12 | 80 ms |
| flat 500 ms | 0/12 | 272 ms |
| **adaptive 300/500 over 1200 ms** | **0/12** | **92 ms** |

**Vocabulary is pushed, not configured.** The sidecar holds no Jarvis state,
so on every connect the Pi sends `{"type":"vocab","words":[…]}` — the music
library's titles and artists plus project names — and whisper biases its
decode toward them (WER 4.6% → 2.7% on whole utterances, no invented terms).
Note this is only safe *because* the endpointing was fixed: on a truncated
fragment the bias dominates and substitutes a library title for what was
actually said, which is the wrong-song failure mode.

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
- `tests/test_voice_noise.py` — the interrupt gate (guitar vs. operator),
  phantom rejection, and the clap curfew's midnight-wrapping window.
- `voicebox/tests/test_pipeline.py` — sidecar-only, two tiers: pure tests for
  the phantom filter and the VAD's evidence arithmetic (run anywhere), and a
  live round trip through architect-tts + silero + whisper that self-skips
  unless the TTS service is up.
- `voicebox/tests/test_pipeline.py` — TTS→VAD→STT round trip (x86 only,
  self-skips without models).

All pure-logic — no model calls, per repo policy.
