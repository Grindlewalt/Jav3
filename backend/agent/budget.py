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


# the active operation's budget; None means unbounded (e.g. a bare model call)
active_budget: contextvars.ContextVar = contextvars.ContextVar("jarvis_budget", default=None)
