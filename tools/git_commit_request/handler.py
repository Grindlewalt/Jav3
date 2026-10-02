from backend.agent.tools.toolctx import require_project
from backend.gitgate import create_request, ensure_repo


async def run(message: str, paths: list[str] | None = None) -> str:
    try:
        slug = await require_project()
    except LookupError as e:
        return f"error: {e}"
    await ensure_repo(slug)
    try:
        row = await create_request(slug, message, paths)
    except ValueError as e:
        return f"error: {e}"
    scope = f" (paths: {', '.join(paths)})" if paths else ""
    replaced = row.get("replaced") or []
    if replaced:
        scope += "; replaces pending " + ", ".join(
            f"request #{r['id']} ({r['message'][:60]!r})" for r in replaced)
    note = "".join(f"\nnote: {n}" for n in row.get("harness_notice") or [])
    return (f"commit request #{row['id']} filed{scope} — status pending. "
            "Nothing is committed or pushed until the operator approves it "
            "in the dashboard." + note)
