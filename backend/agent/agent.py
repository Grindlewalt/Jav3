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

    def spawn(self, sub_brief: str, context_subset: str,
              tools: list[dict] | None = None) -> "Agent":
        """A child agent: narrower context, a brief from this layer, the same
        loop. The orchestrator (backend/orchestrator.py) drives it and collects
        its rollup; this is just the factory the funnel is built on."""
        return Agent(context=context_subset,
                     tools=tools if tools is not None else self.tools,
                     brief=sub_brief)
