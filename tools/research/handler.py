from backend.agent.tools.toolctx import require_project
from backend.research import run_research


async def run(topic: str, angles: int = 4) -> str:
    project = await require_project()
    r = await run_research(topic, project, n_angles=angles)
    lines = [f"Researched '{r['topic']}' (job {r['job_id']})."]
    if r.get("doc_status") == "canonical":
        lines.append(f"Document written to {r['doc_path']} and auto-approved "
                     "(already canonical). Read it with read_file for the details.")
    else:
        lines.append(f"Document staged at {r['doc_path']} (pending operator "
                     "approval). Read it with read_file for the details.")
    return "\n".join(lines)
