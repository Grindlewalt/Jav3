"""Per-request runtime flags carried through the async call chain.

A contextvar propagates within one asyncio task (a single request/stream) and
is invisible to others, so tools deep in the ReAct loop can see request-scoped
state (like ephemeral mode) without threading a parameter through every call.
"""
import contextvars

# True during an incognito/ephemeral turn: nothing is persisted and memory
# writes are redirected to a throwaway dir. Set at the top of the turn.
ephemeral = contextvars.ContextVar("jarvis_ephemeral", default=False)
