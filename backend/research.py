"""Multi-agent research — the Agentic Context Funnel's first real application.

A topic narrows going down: decompose it into distinct angles, run a focused
research subagent per angle (each with only the web tools, minimal context),
and let them coordinate through the shared fetch ledger so no two scrape the
same page. Then summaries flow back up: findings synthesize into one document,
which is staged for approval, and only a tight rollup returns to central so it
never bloats with the hundreds of tool calls underneath.

Subagents run sequentially so the fetch ledger de-dups cleanly (each sees what
the previous ones already pulled) — diversity over raw speed.
"""
from datetime import date

from .agent.loop import run_turn
from .agent.model import confirm_peak, model
from .agent.tools.registry import load_registry, openai_tool_specs
from .db import get_db
from .staging import stage_write

RESEARCH_TOOLS = ("web_search", "web_read")

SUBAGENT_PROMPT = """You are a research subagent with one narrow job: answer the
specific question you are given, using web_search and web_read only.

- Search first, then read the most relevant 1-3 sources. Do not read everything.
- Sources already fetched this session are flagged; pick fresh ones so the
  overall research stays diverse. Do not re-read a flagged source.
- Report concise findings in a few bullet points, each with its source URL.
- Stay strictly on your question. Do not do anything else."""


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


async def _decompose(topic: str, n: int) -> list[str]:
    text = await _complete_text(
        f"Break a research topic into exactly {n} distinct, non-overlapping angles "
        "or sub-questions that together cover it well. Reply with one angle per "
        "line, no numbering, no preamble.",
        f"Topic: {topic}")
    angles = [ln.strip("-*0123456789. ").strip() for ln in text.splitlines() if ln.strip()]
    return angles[:n] or [topic]


async def _project_id(db, project: str):
    async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
        row = await cur.fetchone()
    return row["id"] if row else None


async def _research_angle(angle: str, project: str) -> str:
    """One subagent, narrow context, web tools only. Returns its findings.
    It shares the project's fetch ledger (the web tools key on the active
    project), so it avoids sources earlier subagents already pulled."""
    tools = openai_tool_specs(
        [e for e in load_registry() if e["name"] in RESEARCH_TOOLS])
    db = await get_db()
    try:
        pid = await _project_id(db, project)
        cur = await db.execute(
            "INSERT INTO conversations (project_id, summary) VALUES (?, ?)",
            (pid, f"[research] {angle[:44]}"))
        cid = cur.lastrowid
        await db.commit()
        confirm_peak(cid)  # research is deliberate; don't stall on the peak gate
        final = ""
        async for ev in run_turn(db, cid, SUBAGENT_PROMPT,
                                 [{"role": "user", "content": f"Research question: {angle}"}],
                                 tools=tools):
            if ev["type"] == "final":
                final = ev["content"]
        return final
    finally:
        await db.close()


async def _synthesize(topic: str, findings: list[dict]) -> str:
    joined = "\n\n".join(f"### {f['angle']}\n{f['findings']}" for f in findings)
    body = await _complete_text(
        "Synthesize the research findings below into one clean, well-structured "
        "markdown document. Include a short intro, clear sections, and a final "
        "'Sources' list of the URLs cited. Use only what the findings support; "
        "do not invent facts. No preamble.",
        f"Topic: {topic}\n\nFindings by angle:\n\n{joined}")
    header = f"# Research: {topic}\n\n*Compiled {date.today().isoformat()} by Jarvis research agents.*\n\n"
    return header + body


async def run_research(topic: str, project: str, n_angles: int = 4) -> dict:
    """Decompose -> per-angle subagents -> synthesize -> stage the document.
    Returns a tight rollup (not the raw findings) for central context."""
    n_angles = max(2, min(6, n_angles))
    angles = await _decompose(topic, n_angles)
    findings = []
    for angle in angles:
        findings.append({"angle": angle, "findings": await _research_angle(angle, project)})
    doc = await _synthesize(topic, findings)
    doc_path = f"research/{_slugify(topic)}.md"
    stage_write(project, doc_path, doc.encode())
    return {"topic": topic, "angles": angles, "doc_path": doc_path}
