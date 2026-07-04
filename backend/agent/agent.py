"""The Agent primitive. v3 runs central-only (Jarvis). The funnel — head ->
task-leader -> subagent — is this same object with different context assembly
and a brief written by the layer above; spawn() is the seam it drops into."""
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

    def spawn(self, sub_brief: str, context_subset: str) -> "Agent":
        raise NotImplementedError(
            "multi-agent funnel is a post-v3 seam: spawn() will return an "
            "Agent(context_subset, tools, sub_brief) running the same loop, "
            "reporting back a summary"
        )
