"""Token budget for an operation, shared across every agent in it.

Instead of a hard loop-count cap (which cut real work off early), a run is
bounded by tokens: it loops as long as it needs, until it finishes or the
operation's token budget is spent. One Budget is set on a contextvar at the top
of a chat turn or an agent job; because contextvars propagate into the tasks a
job spawns (asyncio.gather children), every node/subagent shares the same
Budget object — so the cap is "across all agents," exactly.

DeepSeek caches prompt prefixes on disk automatically, which is why the input
cap can be generous: within a loop the prefix is stable and grows, so repeated
iterations mostly hit cache. We track hit/miss here to confirm that.
"""
import contextvars
from dataclasses import dataclass


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    max_input: int
    max_output: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0

    def add(self, usage: dict) -> None:
        if not usage:
            return
        self.input_tokens += usage.get("prompt_tokens", 0)
        self.output_tokens += usage.get("completion_tokens", 0)
        # DeepSeek reports cache hit/miss on the input side (0 if unsupported)
        self.cache_hit += usage.get("prompt_cache_hit_tokens", 0)
        self.cache_miss += usage.get("prompt_cache_miss_tokens", 0)

    def over(self) -> bool:
        return self.input_tokens >= self.max_input or self.output_tokens >= self.max_output

    def summary(self) -> str:
        total = self.cache_hit + self.cache_miss
        ratio = f"{100 * self.cache_hit // total}%" if total else "n/a"
        return (f"{self.input_tokens:,} in / {self.output_tokens:,} out "
                f"(cache hit {ratio})")


# --- operation identity + the budget registry --------------------------------
#
# One operation (a chat turn, a funnel job, a research job) owns one Budget. It
# registers that Budget under an op_id and sets `active_op_id`; the id then
# propagates into the operation's asyncio.gather children (contextvars do), so
# every agent in the operation resolves the SAME Budget by id. The enforcement
# point is a lookup keyed by an explicit id, not a Budget object carried on a
# contextvar — which is what lets Phase 3 pass the id across the VM boundary and
# meter host-side while the loop runs in the guest.
#
# `active_budget` is kept as a fallback for a bare model call or a test that
# sets a Budget directly (no op_id); real operations use register()+active_op_id.
# It goes away once the loop is guest-side and the id is the only mechanism.

active_op_id: contextvars.ContextVar = contextvars.ContextVar("jarvis_op_id", default=None)
active_budget: contextvars.ContextVar = contextvars.ContextVar("jarvis_budget", default=None)

_budgets: dict[str, Budget] = {}


def register(op_id: str, budget: Budget) -> None:
    """Bind a Budget to an operation id for the life of that operation."""
    _budgets[op_id] = budget


def release(op_id: str) -> None:
    """Drop an operation's Budget when it finishes (idempotent)."""
    _budgets.pop(op_id, None)


def get(op_id: str | None) -> Budget | None:
    """The Budget registered under an explicit op_id, if any."""
    return _budgets.get(op_id) if op_id else None


def current() -> Budget | None:
    """The Budget for the operation in scope: resolve the active op_id against
    the registry, else fall back to the legacy active_budget contextvar. None
    means unbounded (e.g. a bare model call)."""
    scoped = _budgets.get(active_op_id.get())
    return scoped if scoped is not None else active_budget.get()
