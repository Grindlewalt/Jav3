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
