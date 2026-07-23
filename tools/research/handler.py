from backend.agent.tools.toolctx import require_project
from backend.research import run_research


async def run(topic: str, angles: int = 4) -> str:
    project = await require_project()
    r = await run_research(topic, project, n_angles=angles)
    return (f"Researched '{r['topic']}' (job {r['job_id']}).\n"
            f"Document written to {r['doc_path']}. Read it with read_file "
            "for the details.")
