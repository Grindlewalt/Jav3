"""Per-request runtime flags carried through the async call chain.

A contextvar propagates within one asyncio task (a single request/stream) and
is invisible to others, so tools deep in the ReAct loop can see request-scoped
state (like ephemeral mode) without threading a parameter through every call.
"""
import contextvars

# True during an incognito/ephemeral turn: nothing is persisted and memory
# writes are redirected to a throwaway dir. Set at the top of the turn.
ephemeral = contextvars.ContextVar("jarvis_ephemeral", default=False)

# Artifact store for a project-less chat turn: the hidden per-conversation
# project slug the file tools fall back to when no project is loaded. An
# explicitly loaded project always wins (see toolctx.require_project).
artifact_slug = contextvars.ContextVar("jarvis_artifact_slug", default=None)

# The bus channel of the chat turn that's running (chat:<cid>), so a job a
# tool launches mid-turn (research, funnel) can announce itself to the chat
# stream — the GUI mounts a live JobTree on the announcement.
event_chan = contextvars.ContextVar("jarvis_event_chan", default=None)

# Fetch-ledger scope for the running operation (a chat turn, an agent run, a
# funnel job). The ledger dedups parallel readers WITHIN one operation; keying
# it by project (the old fallback) made claims permanent, so a scheduled run
# could never re-read a page any earlier turn in the project had touched.
web_session = contextvars.ContextVar("jarvis_web_session", default=None)

# The project this operation is PINNED to, resolved at turn start (a chat's
# assigned project, an agent run's explicit project, a schedule's project_slug —
# falling back to the GUI's global active project). Tools prefer this over the
# DB global (toolctx.active_slug), which is what lets turns in different
# projects run concurrently without stomping each other. UNSET (the default)
# means "not inside a pinned operation" — distinct from None, which means the
# operation resolved to no project at all (artifact-store chat).
ACTIVE_UNSET = object()
active_project = contextvars.ContextVar("jarvis_active_project",
                                        default=ACTIVE_UNSET)

# The browser tab this turn was asked from (backend/gui.py's tab registry), so
# a tool that puts something on a screen or through speakers can pick the right
# machine. Without it "play something" started the track in every open Jarvis
# tab at once — laptop, desktop and phone, slightly out of sync. None means the
# turn has no asking tab (a schedule, an agent run), and the tool falls back to
# the most recently used one rather than to all of them.
gui_tab = contextvars.ContextVar("jarvis_gui_tab", default=None)

# The conversation the running turn belongs to, so a tool that rebinds the
# project mid-conversation (load_project) can pin the change onto the
# conversation row instead of yanking the global session state.
conversation_id = contextvars.ContextVar("jarvis_conversation_id", default=None)

# How many spawn_agent hops deep the current operation is (head chat = 0; the
# spawn_agent handler increments around each child run). Read when a headless
# agent's toolset is built: below autonomy.MAX_SPAWN_DEPTH the child gets
# spawn_agent back, at the cap it becomes a leaf.
spawn_depth = contextvars.ContextVar("jarvis_spawn_depth", default=0)

# Taint stamp for a memory_write happening in an operation that has ALREADY
# consumed untrusted external content (web/research). The broker sets this to
# "untrusted" before brokering such a write; the memory_write handler stamps
# `taint: untrusted` into the note's frontmatter so it is quarantined out of
# binding context (memory.note_trusted) until the operator promotes it. This is
# the persisted half of the broker's runtime taint ledger — the tag survives on
# the file instead of only annotating the in-turn result.
write_taint = contextvars.ContextVar("jarvis_write_taint", default=None)
