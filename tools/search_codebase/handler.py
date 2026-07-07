from backend.codeindex import search_code
from backend.agent.tools.toolctx import require_project

CAP = 50


async def run(query: str, subdir: str | None = None, regex: bool = False) -> str:
    slug = await require_project()
    try:
        hits = search_code(slug, query, subdir=subdir, regex=regex,
                           max_results=CAP + 1)
    except ValueError as e:
        return f"error: {e}"
    if not hits:
        return "no matches"
    truncated = len(hits) > CAP
    lines = [f"{h['path']}:{h['line_no']}: {h['line']}" for h in hits[:CAP]]
    if truncated:
        lines.append(f"{CAP} matches (truncated — narrow the query)")
    return "\n".join(lines)
