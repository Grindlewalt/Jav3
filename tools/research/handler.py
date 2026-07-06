from backend.agent.tools.toolctx import require_project
from backend.research import run_research


async def run(topic: str, angles: int = 4) -> str:
    project = await require_project()
    r = await run_research(topic, project, n_angles=angles)
    lines = [f"Researched '{r['topic']}' across {len(r['angles'])} angles:"]
    lines += [f"  - {a}" for a in r["angles"]]
    lines.append(f"\nFull write-up staged at {r['doc_path']} (pending your approval). "
                 "Read it with read_file for the details.")
    return "\n".join(lines)
