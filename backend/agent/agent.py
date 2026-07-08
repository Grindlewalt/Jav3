"""The Agent primitive: an assembled context plus an optional brief from the
layer above. Central Jarvis is one with no brief; the funnel's nodes
(backend/orchestrator.py) are the same object with narrowed context."""
from dataclasses import dataclass, field


@dataclass
class Agent:
    context: str                      # assembled system prompt
    tools: list[dict] = field(default_factory=list)
    brief: str | None = None          # written by the layer above; None for Jarvis

    def system_prompt(self) -> str:
        if self.brief:
            return f"{self.context}\n\n---\n\n# Your brief\n{self.brief}"
        return self.context
