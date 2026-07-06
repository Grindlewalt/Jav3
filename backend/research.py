"""Multi-agent research — the funnel's first application, now on the M7
orchestrator (backend/orchestrator.py).

Research is a HEAD with a "research this topic" brief. The orchestrator's head
decomposes the topic into angles, spawns a subagent per angle (running the web
tools in parallel, coordinating through the claim-based fetch ledger so no two
pull the same page), and this module's `_synthesize` is the head's deliverable:
it turns the subagents' findings into one cited document, staged for approval.
Only a tight rollup returns to central; the full node tree is retained and
walkable.
"""
import uuid
from datetime import date

from . import orchestrator
from .agent.loop import _enforce_rules
from .agent.model import model
from .agent.tools.registry import load_registry, openai_tool_specs
from .memory import standing_rules_tail
from .staging import stage_write

RESEARCH_TOOLS = ("web_search", "web_read")


async def _complete_text(system: str, user: str, temperature: float = 0.3) -> str:
    parts = []
    async for ev in model.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
    ):
        if ev["type"] == "message":
            parts.append(ev["content"])
    return "".join(parts).strip()


def _slugify(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50] or "topic")


async def _synthesize(topic: str, findings: list[str]) -> str:
    joined = "\n\n".join(f"### Finding {i + 1}\n{f}" for i, f in enumerate(findings) if f)
    body = await _complete_text(
        "Synthesize the research findings below into one clean, well-structured "
        "markdown document. Include a short intro, clear sections, and a final "
        "'Sources' list of the URLs cited. Use only what the findings support; "
        "do not invent facts. No preamble.",
        f"Topic: {topic}\n\nFindings:\n\n{joined}")
    header = (f"# Research: {topic}\n\n*Compiled {date.today().isoformat()} by "
              "Jarvis research agents.*\n\n")
    doc = header + body
    # the operator reads/approves this document, so enforce their rules on it
    rules = standing_rules_tail()
    return await _enforce_rules(doc, rules) if rules else doc


def _web_tools():
    return openai_tool_specs(
        [e for e in load_registry() if e["name"] in RESEARCH_TOOLS])


async def run_research(topic: str, project: str, n_angles: int = 4,
                       job_id: str | None = None) -> dict:
    """Run a research job through the orchestrator. Returns a tight rollup dict.
    `job_id` is passed by the streaming endpoint (so it can subscribe first);
    the `research` tool lets it mint one."""
    n_angles = max(2, min(6, n_angles))
    job_id = job_id or uuid.uuid4().hex
    doc_path = f"research/{_slugify(topic)}.md"

    async def deliverable(child_outputs: list[str]) -> str:
        doc = await _synthesize(topic, child_outputs)
        stage_write(project, doc_path, doc.encode())
        return doc_path

    brief = (f"Research this topic and produce a cited synthesis document: {topic}. "
             f"Break it into about {n_angles} distinct, non-overlapping angles; "
             f"research each using web_search and web_read; cite sources.")
    result = await orchestrator.run_job(
        job_id, brief, project, peak=True, leaf_tools=_web_tools(),
        deliverable=deliverable, title=f"Research: {topic}")
    return {"topic": topic, "job_id": job_id, "root_id": result["root_id"],
            "doc_path": result.get("doc_path") or doc_path}
