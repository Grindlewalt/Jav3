"""Voice desktop mode: the orchestrator between the browser (/voice page)
and the voicebox inference sidecar.

The Pi never touches audio content — mic PCM relays browser→sidecar, TTS PCM
relays sidecar→browser, both as opaque bytes. What lives here is the
conversational state machine:

  LISTENING      no turn; mic flows; queued work may drain
  THINKING       a turn is running, nothing speakable emitted yet
  SPEAKING       TTS chunks queued/streaming (the turn may still be running)
  BARGE_PENDING  the browser's local VAD paused playback; awaiting the
                 sidecar's transcript verdict (empty = false alarm, resume)
  CONFIRM_PEAK   the peak-pricing question was spoken; next utterance is the
                 yes/no answer (channel plumbing — not persisted)

Turns are ordinary chat turns: the session inserts the user row, runs the
same peak gate as POST /api/chat, and calls chat.start_turn — persistence,
budget, project pinning and compaction are all the chat path's. The bus
channel is subscribed BEFORE the turn starts (the existing discipline) and a
separate pump task feeds the sidecar so the bus consumer never blocks on
socket I/O.

Barge-in: the browser already paused playback locally the instant its VAD
fired, so nothing here is latency-critical. Real speech → tts_cancel to the
sidecar, stop_playback to the browser, and the turn is cancelled with an
interrupt note recording exactly what was heard (chat.set_interrupt_note);
noise → resume_playback and the pause was a sub-second hiccup. While the
verdict is pending the TTS stream keeps flowing — the browser buffers it, so
a false alarm loses nothing.

Talk-while-working (turn already ran a tool): the turn is left running and
the utterance is parked; phase 4 replaces the parking with the clone-twin
flow."""
import asyncio
import contextlib
import json
import logging
import struct

import websockets

from . import bus, chat
from .agent.model import confirm_peak, in_peak_window, peak_confirmed
from .config import settings
from .db import get_db, open_conversation
from .memory import get_active_project
from .voice_text import (CUTOFF_MARK, CUTOFF_NOTHING, ESCALATE_PREFIX,
                         SpeechChunker, annotate_cutoff, heard_upto_note,
                         spoken_fraction)

log = logging.getLogger(__name__)

MIC_FRAME = 0x01
TTS_FRAME = 0x02
TTS_BYTES_PER_MS = 48                  # PCM16 mono @ 24 kHz

LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
BARGE_PENDING = "barge_pending"
CONFIRM_PEAK = "confirm_peak"
CONFIRM_ESCALATE = "confirm_escalate"
CONFIRM_STATES = (CONFIRM_PEAK, CONFIRM_ESCALATE)

AFFIRMATIVE = ("yes", "yeah", "yep", "sure", "go ahead", "continue",
               "do it", "okay", "ok", "please do", "send it")

# operator override: these words anywhere in an utterance route the turn
# straight to DeepSeek, skipping the local tier and its permission ask
SMART_WORDS = ("smart model", "deepseek", "big model")

# The slim context for local turns. An 8B in a 4-8k window can't carry the
# full sandwich (and prefilling it costs seconds on a 3060) — it keeps soul,
# user, env and the active project; the heavyweight indexes and the full
# standing-memory notes are dropped. The operator-rules tail survives any
# exclude by design (memory.assemble_system_prompt).
LOCAL_CONTEXT_EXCLUDE = ("standing-memory", "all-projects.md", "agents-index",
                         "secrets-index", "computers-index")

# The local tier's toolset: conversation + media control + a quick lookup.
# Thirty tool schemas would drown an 8B (and slow its prefill); anything
# beyond these is exactly what escalation exists for.
LOCAL_TOOLS = ("play_music", "music_control", "music_search", "music_status",
               "play_movie", "computer_play", "computer_playback",
               "computer_status", "computer_volume", "web_search", "web_read")

PEAK_ASK = "Heads up: peak pricing is in effect. Should I continue?"
PEAK_DROPPED = "Okay, I'll hold off. Say it again later if you want it."
ESCALATE_ASK = "Want me to send it up?"
ESCALATE_DROPPED = "Okay, leaving it."
BUSY_LINE = "One second, I'm still working on that. I'll take this next."
CAP_LINE = ("One second — my hands are completely full. I'll take that "
            "as soon as something finishes.")

TWIN_NOTE = ('[system note: this conversation was just cloned for voice mode. '
             'Your twin — an identical agent with this same history — is '
             'still working on: "{task}". Its result will be delivered into '
             'this conversation when it finishes; do not redo that work. '
             'Of its in-progress spoken reply, the operator heard: '
             '"{spoken}". You are now the one talking. Continue naturally.]')
TWIN_ACK = "Understood."
DELIVERY_NOTE = ('[background result — the twin that was working on '
                 '"{task}" just finished:]\n\n{final}')
DELIVERY_ACK = "Got it — noted above."


async def clone_conversation(db, old_cid: int, *, task: str, spoken: str) -> int:
    """Mint the talking twin: a new kind='chat' conversation carrying the
    exact model-facing history of `old_cid`. Rides the compaction checkpoint
    instead of fighting it — the parent's summary is copied and compact_upto
    reset to 0, so copying only the post-checkpoint messages reproduces what
    compaction.assemble would have built for the parent. tool_calls rows come
    along so the GUI's activity view stays whole. One transaction."""
    async with db.execute(
        "SELECT project_id, project_locked, summary, compact_summary, "
        "compact_upto FROM conversations WHERE id = ?", (old_cid,)) as cur:
        old = await cur.fetchone()
    if old is None:
        raise ValueError(f"no conversation {old_cid}")
    new_cid = await open_conversation(
        db, project=None, title=f"{old['summary'] or 'chat'} (voice)",
        kind="chat", parent=old_cid, commit=False)
    await db.execute(
        "UPDATE conversations SET project_id = ?, project_locked = ?, "
        "compact_summary = ?, compact_upto = 0 WHERE id = ?",
        (old["project_id"], old["project_locked"],
         old["compact_summary"], new_cid))
    upto = old["compact_upto"] or 0
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) "
        "SELECT ?, role, content, created_at FROM messages "
        "WHERE conversation_id = ? AND id > ? ORDER BY id",
        (new_cid, old_cid, upto))
    await db.execute(
        "INSERT INTO tool_calls (conversation_id, tool, args, result, created_at) "
        "SELECT ?, tool, args, result, created_at FROM tool_calls "
        "WHERE conversation_id = ? ORDER BY id", (new_cid, old_cid))
    note = TWIN_NOTE.format(task=task[:200], spoken=spoken or "nothing yet")
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (new_cid, note))
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content) "
        "VALUES (?, 'assistant', ?)", (new_cid, TWIN_ACK))
    await db.commit()
    return new_cid


class SidecarLink:
    """One persistent WS to the voicebox, with the computeruse client's
    reconnect discipline (exponential backoff, 20 s pings)."""

    def __init__(self, on_json, on_bytes) -> None:
        self._on_json = on_json
        self._on_bytes = on_bytes
        self._ws = None
        self._task: asyncio.Task | None = None
        self.ready = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        delay = 1
        headers = {"Authorization": f"Bearer {settings.voice_sidecar_token}"}
        while True:
            try:
                async with websockets.connect(
                        settings.voice_sidecar_url,
                        additional_headers=headers,
                        ping_interval=20, ping_timeout=20,
                        max_size=2 ** 20) as ws:
                    self._ws = ws
                    delay = 1
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            await self._on_bytes(msg)
                        else:
                            ev = json.loads(msg)
                            if ev.get("type") == "ready":
                                self.ready.set()
                            await self._on_json(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect covers it
                log.warning("voicebox link lost: %s", exc)
            self._ws = None
            self.ready.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def send_json(self, obj: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(obj))
            return True
        except Exception:  # noqa: BLE001 — the rx loop handles the reconnect
            return False

    async def send_bytes(self, data: bytes) -> bool:
        ws = self._ws
        if ws is None:
            return False                # mic audio while offline: just dropped
        try:
            await ws.send(data)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class VoiceSession:
    """One /voice browser connection. Transports are injected as callables so
    tests can drive the whole machine with queues."""

    def __init__(self, send_json, send_bytes) -> None:
        self._send_json = send_json      # async (dict) -> browser
        self._send_bytes = send_bytes    # async (bytes) -> browser
        self.link = SidecarLink(self._on_sidecar_json, self._on_sidecar_bytes)

        self.state = LISTENING
        self.cid: int | None = None
        self.tab: str | None = None
        self.project: str | None = None
        self.project_mode: str | None = None
        self.muted = False

        # per-turn
        self.turn_task: asyncio.Task | None = None
        self.turn_consumer: asyncio.Task | None = None
        self.turn_q = None
        self.turn_saw_tool = False
        self.turn_done = True
        self.chunker: SpeechChunker | None = None
        self.raw_text = ""               # concatenated token deltas this turn

        # speech chunks (turn + system lines share the id space)
        self.next_chunk = 1
        self.chunks: dict[int, dict] = {}   # id -> {text, bytes, dur_ms, played, system}
        self.order: list[int] = []          # emit order, current turn only
        self.barge_pos: tuple[int, int] | None = None

        self.pending_peak: dict | None = None    # {text, smart, insert}
        self.pending_escalate: str | None = None  # utterance awaiting send-up
        self.queued: list[str] = []      # transcripts parked while busy
        self.turn_user_msg = ""          # what started the running turn
        self.turn_local = False          # this turn runs on the local tier
        self._hold: str | None = None    # opening tokens held back until we
        self._escalating = False         # know the reply isn't [ESCALATE]
        self.workers: dict[int, dict] = {}   # worker cid -> {task, watcher, …}
        self.pending_deliveries: list[str] = []   # spoken digests, FIFO
        self.dead = False                # browser gone: keep DB work, stop audio

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self.link.start()
        try:
            await asyncio.wait_for(self.link.ready.wait(), timeout=8)
            await self._send_json({"type": "ready"})
        except asyncio.TimeoutError:
            await self._send_json(
                {"type": "error", "message": "voicebox offline — check the "
                 "sidecar on the main server; retrying in the background"})
            # keep going: the link keeps retrying, ready fires when it lands

    async def close(self) -> None:
        self.dead = True
        for t in (self.turn_consumer,):
            if t:
                t.cancel()
        # the turn is NOT cancelled (closing the tab must not kill in-flight
        # work — same rule as the chat page), and worker watchers stay up so
        # a twin that finishes after the tab closed still lands its result
        # in the transcript; `dead` just silences the speech side.
        await self.link.stop()

    # ---- browser -> session -------------------------------------------------

    async def on_browser_bytes(self, data: bytes) -> None:
        if data[:1] == bytes([MIC_FRAME]) and not self.muted:
            await self.link.send_bytes(data)

    async def on_browser_json(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "hello":
            self.tab = msg.get("tab") or None
            self.project = msg.get("project") or None
            self.project_mode = msg.get("project_mode") or None
            cid = msg.get("conversation_id")
            self.cid = int(cid) if cid else None
            await self._push_state()
        elif kind == "barge_in":
            if self.state == SPEAKING:
                self.state = BARGE_PENDING
                self.barge_pos = (int(msg.get("chunk_id", 0)),
                                  int(msg.get("played_ms", 0)))
                await self._push_state()
        elif kind == "chunk_played":
            c = self.chunks.get(int(msg.get("chunk_id", 0)))
            if c:
                c["played"] = True
            await self._maybe_idle()
        elif kind == "mute":
            self.muted = bool(msg.get("on"))
        elif kind == "end_session":
            await self.close()

    # ---- sidecar -> session -------------------------------------------------

    async def _on_sidecar_bytes(self, frame: bytes) -> None:
        if frame[:1] == bytes([TTS_FRAME]) and len(frame) >= 5:
            (chunk_id,) = struct.unpack("<I", frame[1:5])
            c = self.chunks.get(chunk_id)
            if c is not None:
                c["bytes"] += len(frame) - 5
            await self._send_bytes(frame)

    async def _on_sidecar_json(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "ready":
            await self._send_json({"type": "ready"})
        elif kind in ("speech_start", "speech_end"):
            await self._send_json(ev)       # UI listening indicator
        elif kind == "transcript":
            await self._on_transcript(ev.get("text") or "")
        elif kind == "tts_done":
            c = self.chunks.get(ev.get("id"))
            if c is not None:
                c["dur_ms"] = ev.get("dur_ms")
            await self._send_json({"type": "tts_end", "chunk_id": ev.get("id"),
                                   "dur_ms": ev.get("dur_ms")})
        elif kind == "error":
            await self._send_json(ev)

    # ---- the transcript router (the heart of the machine) --------------------

    async def _on_transcript(self, text: str) -> None:
        text = text.strip()

        if self.state == BARGE_PENDING:
            self.state = SPEAKING
            pos, self.barge_pos = self.barge_pos, None
            if not text:                              # cough / noise
                await self._send_json({"type": "resume_playback"})
                await self._push_state()
                return
            # real speech: kill the audio path, then route below
            await self.link.send_json({"type": "tts_cancel"})
            await self._send_json({"type": "stop_playback"})
            await self._send_json({"type": "transcript", "text": text})
            await self._route_speech(text, barge_pos=pos)
            return

        if not text:
            return                                    # noise while quiet

        await self._send_json({"type": "transcript", "text": text})

        if self.state in CONFIRM_STATES:
            # if the ask's audio was still playing, the answer moots it
            await self._send_json({"type": "stop_playback"})
            if self.state == CONFIRM_PEAK:
                await self._on_peak_answer(text)
            else:
                await self._on_escalate_answer(text)
            return

        await self._route_speech(text, barge_pos=None)

    async def _route_speech(self, text: str,
                            barge_pos: tuple[int, int] | None) -> None:
        turn_running = self.turn_task is not None and not self.turn_task.done()

        if turn_running and self.turn_saw_tool:
            # mid-task: never interrupt real work. Under the cap the working
            # turn is left running as a background twin and the conversation
            # is cloned for talking; at the cap the utterance waits its turn.
            if len(self.workers) >= settings.voice_max_workers:
                self.queued.append(text)
                await self._send_json({"type": "queued", "text": text})
                await self._speak_system(CAP_LINE)
            else:
                await self._background_and_clone(text, barge_pos)
            return

        if turn_running:
            # pure prose — classic barge-in: record what was heard, cancel.
            # Settle the consumer too (its final arrives right behind the
            # cancel) so it can't stomp the next turn's fresh state.
            spoken = self._spoken_through(barge_pos)
            chat.set_interrupt_note(self.cid, annotate_cutoff(spoken))
            self.turn_task.cancel()
            await asyncio.wait([self.turn_task], timeout=10)
            if self.turn_consumer:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.turn_consumer, timeout=2)
            await self._begin_turn(text)
            return

        if barge_pos is not None and self.cid is not None:
            # turn already finished but its audio tail was cut — annotate the
            # persisted row with how far the operator actually listened
            spoken = self._spoken_through(barge_pos)
            if spoken:
                await self._annotate_last_reply(heard_upto_note(spoken))

        await self._begin_turn(text)

    # ---- talk-while-working ----------------------------------------------------

    async def _background_and_clone(self, text: str,
                                    barge_pos: tuple[int, int] | None) -> None:
        """The running turn becomes a background worker on the OLD
        conversation (it is already a detached task — nothing to move); the
        session rebinds to a full-history clone that knows about its twin."""
        old_cid = self.cid
        task_hint = " ".join(self.turn_user_msg.split())[:200]
        spoken = self._spoken_through(barge_pos)

        # stop routing the old turn's audio to the voice channel: fresh
        # subscription first (no event gap), then retire the old consumer
        watch_q = bus.subscribe(chat._chan(old_cid))
        if self.turn_consumer and not self.turn_consumer.done():
            self.turn_consumer.cancel()

        db = await get_db()
        try:
            new_cid = await clone_conversation(
                db, old_cid, task=task_hint, spoken=spoken)
        finally:
            await db.close()

        watcher = asyncio.create_task(
            self._watch_worker(old_cid, watch_q, task_hint))
        self.workers[old_cid] = {"task": task_hint, "watcher": watcher,
                                 "status": "working"}
        self.cid = new_cid
        await self._send_json({"type": "conversation", "id": new_cid,
                               "reason": "cloned"})
        await self._push_workers()
        await self._begin_turn(text)

    async def _watch_worker(self, worker_cid: int, q, task_hint: str) -> None:
        """Follow a backgrounded twin to its final; deliver the result into
        the talking conversation (durable rows first, speech when idle)."""
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "final":
                    await self._deliver_worker(worker_cid, task_hint,
                                               ev.get("content") or "")
                    return
                if ev.get("type") in ("error", "job_end"):
                    await self._deliver_worker(
                        worker_cid, task_hint,
                        f"(the background task ended without a result: "
                        f"{ev.get('message', 'stream closed')})")
                    return
        finally:
            bus.unsubscribe(chat._chan(worker_cid), q)

    async def _deliver_worker(self, worker_cid: int, task_hint: str,
                              final: str) -> None:
        w = self.workers.pop(worker_cid, None)
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES (?, 'user', ?)",
                (self.cid, DELIVERY_NOTE.format(task=task_hint, final=final)))
            await db.execute(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES (?, 'assistant', ?)", (self.cid, DELIVERY_ACK))
            await db.commit()
        finally:
            await db.close()
        if self.dead:
            return                        # transcript has it; nobody to tell
        await self._push_workers()
        await self._send_json({"type": "worker_done",
                               "conversation_id": worker_cid,
                               "task": (w or {}).get("task", task_hint)})
        digest = self._delivery_digest(task_hint, final)
        self.pending_deliveries.append(digest)
        await self._maybe_idle()

    @staticmethod
    def _delivery_digest(task_hint: str, final: str) -> str:
        """What gets SAID about a finished background task. Short results are
        spoken whole; long ones get a sentence-bounded lead-in — the full
        text is already in the transcript (no extra model call: token
        thrift)."""
        flat = " ".join(final.split())
        if len(flat) <= 400:
            return f"Done with the earlier task. {flat}"
        cut = flat.rfind(". ", 0, 380)
        lead = flat[:cut + 1] if cut > 0 else flat[:380]
        return (f"Done with the earlier task. {lead} "
                "The full result is in the transcript.")

    async def _push_workers(self) -> None:
        await self._send_json({"type": "workers", "list": [
            {"conversation_id": cid, "task": w["task"], "status": w["status"]}
            for cid, w in self.workers.items()]})

    # ---- turns ---------------------------------------------------------------

    async def _begin_turn(self, text: str, *, smart: bool = False,
                          insert: bool = True) -> None:
        """Start a turn. Routing: with a local tier configured, turns run on
        the operator's ollama unless `smart` (an escalation rerun) or the
        utterance names the smart model outright. `insert=False` reruns an
        utterance whose user row already exists (escalation)."""
        if any(w in text.lower() for w in SMART_WORDS):
            smart = True
        local = bool(settings.voice_local_model) and not smart
        db = await get_db()
        try:
            await self._ensure_conversation(db, text)
            if self.cid in chat._active_turns:
                # a foreign turn (chat page) is running this conversation
                self.queued.append(text)
                await self._send_json({"type": "queued", "text": text})
                await self._speak_system(BUSY_LINE)
                return
            # local inference is free at any hour — only DeepSeek is gated
            if not local and in_peak_window() and not peak_confirmed(self.cid):
                self.pending_peak = {"text": text, "smart": smart,
                                     "insert": insert}
                self.state = CONFIRM_PEAK
                await self._push_state()
                await self._speak_system(PEAK_ASK)
                return
            if insert:
                await db.execute(
                    "INSERT INTO messages (conversation_id, role, content) "
                    "VALUES (?, 'user', ?)", (self.cid, text))
                await db.commit()
        finally:
            await db.close()

        self._reset_turn_state()
        self.turn_user_msg = text
        self.turn_local = local
        self._hold = "" if local else None   # watch for an [ESCALATE] opener
        self.turn_q = bus.subscribe(chat._chan(self.cid))
        self.turn_task = chat.start_turn(
            self.cid, user_msg=text, tab=self.tab, voice=True,
            model_name=settings.voice_local_model if local else None,
            base_url=settings.voice_local_base_url if local else None,
            context_exclude=LOCAL_CONTEXT_EXCLUDE if local else (),
            tools_only=LOCAL_TOOLS if local else ())
        self.turn_consumer = asyncio.create_task(self._consume_turn())
        self.state = THINKING
        await self._push_state()

    def _reset_turn_state(self) -> None:
        # a lingering consumer from the previous turn must not touch the new
        # turn's state (it may be THIS task when a drain chains turns — never
        # cancel ourselves)
        cur = asyncio.current_task()
        if (self.turn_consumer and self.turn_consumer is not cur
                and not self.turn_consumer.done()):
            self.turn_consumer.cancel()
        self.turn_saw_tool = False
        self.turn_done = False
        self.chunker = SpeechChunker()
        self.raw_text = ""
        self.order = []
        self.barge_pos = None
        self._escalating = False
        self._hold = None
        if len(self.chunks) > 400:       # prune long-session bookkeeping
            for i in sorted(self.chunks)[:-200]:
                del self.chunks[i]

    async def _consume_turn(self) -> None:
        q, cid = self.turn_q, self.cid
        try:
            while True:
                ev = await q.get()
                kind = ev.get("type")
                if kind == "token":
                    text = ev.get("text", "")
                    self.raw_text += text
                    text = self._filter_escalate(text)
                    if text is None:
                        continue
                    for sentence in self.chunker.feed(text):
                        await self._enqueue_speech(sentence)
                elif kind == "tool":
                    self.turn_saw_tool = True
                    await self._send_json({"type": "tool_activity",
                                           "name": ev.get("name"), "phase": "call"})
                elif kind == "tool_result":
                    await self._send_json({"type": "tool_activity",
                                           "name": ev.get("name"),
                                           "phase": "result", "ok": ev.get("ok")})
                elif kind == "final":
                    await self._finish_turn(ev.get("content") or "")
                    break
                elif kind in ("error", "job_end"):
                    self.turn_done = True
                    if kind == "error":
                        await self._send_json(ev)
                    await self._maybe_idle()
                    break
        finally:
            bus.unsubscribe(chat._chan(cid), q)

    def _filter_escalate(self, text: str) -> str | None:
        """Local turns hold their opening tokens until it's clear the reply
        isn't an [ESCALATE] line — that marker is protocol, never speech.
        Returns text to speak now (may include the released hold), or None
        while still deciding / once escalation is confirmed."""
        if self._escalating:
            return None
        if self._hold is None:
            return text
        self._hold += text
        lead = self._hold.lstrip()
        if lead.startswith(ESCALATE_PREFIX):
            self._escalating = True
            self._hold = None
            return None
        if ESCALATE_PREFIX.startswith(lead[:len(ESCALATE_PREFIX)]):
            return None                  # still a possible prefix — keep holding
        released, self._hold = self._hold, None
        return released

    async def _finish_turn(self, final: str) -> None:
        """Heal streamed-vs-final drift, then flush the chunker. Voice turns
        skip the rules rewrite, so normally final == raw_text and only the
        remainder flushes; if the bus shed tokens under load, speak the
        missing suffix of the final text instead."""
        if self._escalating or final.lstrip().startswith(ESCALATE_PREFIX):
            await self._propose_escalation(final)
            return
        interrupted = (chat.INTERRUPTED_MARKER in final
                       or CUTOFF_MARK in final or final == CUTOFF_NOTHING)
        if not interrupted:
            if final.startswith(self.raw_text) and len(final) > len(self.raw_text):
                for sentence in self.chunker.feed(final[len(self.raw_text):]):
                    await self._enqueue_speech(sentence)
            for sentence in self.chunker.flush():
                await self._enqueue_speech(sentence)
        self.turn_done = True
        await self._maybe_idle()

    async def _propose_escalation(self, final: str) -> None:
        """The local model bounced the request. Scrub the protocol line from
        the transcript (the history must end at the operator's utterance so
        the smart rerun sees a clean turn), then ask out loud."""
        reason = final.lstrip()[len(ESCALATE_PREFIX):].strip() \
            if final.lstrip().startswith(ESCALATE_PREFIX) else ""
        db = await get_db()
        try:
            await db.execute(
                "DELETE FROM messages WHERE id = ("
                "SELECT id FROM messages WHERE conversation_id = ? AND "
                "role = 'assistant' ORDER BY id DESC LIMIT 1) "
                "AND content LIKE ?",
                (self.cid, ESCALATE_PREFIX + "%"))
            await db.commit()
        finally:
            await db.close()
        self.pending_escalate = self.turn_user_msg
        self.turn_done = True
        self.state = CONFIRM_ESCALATE
        await self._push_state()
        ask = f"{reason} {ESCALATE_ASK}" if reason else \
            f"That needs the smart model. {ESCALATE_ASK}"
        await self._speak_system(ask)

    async def _on_escalate_answer(self, text: str) -> None:
        pending, self.pending_escalate = self.pending_escalate, None
        if any(a in text.lower() for a in AFFIRMATIVE):
            self.state = LISTENING
            await self._begin_turn(pending, smart=True, insert=False)
        else:
            self.state = LISTENING
            await self._push_state()
            await self._speak_system(ESCALATE_DROPPED)

    # ---- speech out -----------------------------------------------------------

    async def _enqueue_speech(self, sentence: str, system: bool = False) -> None:
        chunk_id = self.next_chunk
        self.next_chunk += 1
        self.chunks[chunk_id] = {"text": sentence, "bytes": 0, "dur_ms": None,
                                 "played": False, "system": system}
        if not system:
            self.order.append(chunk_id)
        await self._send_json({"type": "assistant_text", "chunk_id": chunk_id,
                               "text": sentence, "system": system})
        sent = await self.link.send_json(
            {"type": "tts", "id": chunk_id, "text": sentence})
        if not sent:
            # no sidecar: mark it played so idle detection still works — the
            # text is on screen, the conversation stays functional
            self.chunks[chunk_id]["played"] = True
        if not system and self.state in (THINKING, LISTENING):
            self.state = SPEAKING
            await self._push_state()

    async def _speak_system(self, line: str) -> None:
        await self._enqueue_speech(line, system=True)

    def _spoken_through(self, barge_pos: tuple[int, int] | None) -> str:
        """Everything of the CURRENT turn the operator heard: fully played
        chunks, plus the played fraction of the chunk the cut landed in."""
        if barge_pos is None:
            texts = [self.chunks[i]["text"] for i in self.order
                     if self.chunks[i]["played"]]
            return " ".join(texts)
        cut_id, played_ms = barge_pos
        texts = []
        for i in self.order:
            if i < cut_id:
                texts.append(self.chunks[i]["text"])
            elif i == cut_id:
                c = self.chunks[i]
                dur = c["dur_ms"] or (c["bytes"] // TTS_BYTES_PER_MS) or 1
                frac = spoken_fraction(c["text"], played_ms, dur)
                if frac:
                    texts.append(frac)
        return " ".join(texts)

    # ---- plumbing --------------------------------------------------------------

    async def _on_peak_answer(self, text: str) -> None:
        pending, self.pending_peak = self.pending_peak, None
        lowered = text.lower()
        if any(a in lowered for a in AFFIRMATIVE):
            confirm_peak(self.cid)
            self.state = LISTENING
            await self._begin_turn(pending["text"], smart=pending["smart"],
                                   insert=pending["insert"])
        else:
            self.state = LISTENING
            await self._push_state()
            await self._speak_system(PEAK_DROPPED)

    async def _ensure_conversation(self, db, first_text: str) -> None:
        if self.cid is not None:
            return
        mode = self.project_mode or ("pin" if self.project else "follow")
        if mode == "pin" and self.project:
            async with db.execute(
                "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
                (self.project,)) as cur:
                if not await cur.fetchone():
                    mode, self.project = "follow", None
        if mode == "pin" and self.project:
            active = self.project
        elif mode == "none":
            active = None
        else:
            active = await get_active_project(db)
        title = " ".join(first_text.split())[:48] or "(voice)"
        self.cid = await open_conversation(
            db, project=active, title=title, locked=mode != "follow")
        await self._send_json({"type": "conversation", "id": self.cid,
                               "reason": "opened"})

    async def _annotate_last_reply(self, note: str) -> None:
        db = await get_db()
        try:
            await db.execute(
                "UPDATE messages SET content = content || ? WHERE id = ("
                "SELECT id FROM messages WHERE conversation_id = ? AND "
                "role = 'assistant' ORDER BY id DESC LIMIT 1)",
                (note, self.cid))
            await db.commit()
        finally:
            await db.close()

    async def _maybe_idle(self) -> None:
        """LISTENING once the turn is over and every non-system chunk of it
        has been played; then drain anything that queued up meanwhile. Never
        fires out of a pending question — a played confirm-ask chunk must not
        flip the state from under the expected yes/no."""
        if not self.turn_done or self.state == BARGE_PENDING \
                or self.state in CONFIRM_STATES:
            return
        if any(not self.chunks[i]["played"] for i in self.order):
            return
        self.state = LISTENING
        await self._push_state()
        if self.queued:                     # parked speech outranks announcements
            await self._begin_turn(self.queued.pop(0))
        elif self.pending_deliveries:
            await self._speak_system(self.pending_deliveries.pop(0))

    async def _push_state(self) -> None:
        turn_running = self.turn_task is not None and not self.turn_task.done()
        await self._send_json({"type": "state", "state": self.state,
                               "turn_working": turn_running and self.turn_saw_tool,
                               "conversation_id": self.cid})
