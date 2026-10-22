"""skill_request: file a skill install request (host-side, brokered).

Validation and the table live in backend/skillrequests.py. Nothing is fetched
here: the operator's Fetch button is the only thing that contacts the source."""
from backend import runtime, skillrequests
from backend.agent.tools.toolctx import active_slug
from backend.db import get_db


async def run(source: str = "", reason: str = "", name: str | None = None) -> str:
    slug = await active_slug()
    db = await get_db()
    try:
        row = await skillrequests.file_request(
            db, source=source, name=name, reason=reason, project=slug,
            conversation_id=runtime.conversation_id.get())
    except skillrequests.SkillRequestError as e:
        return f"error: {e}"
    finally:
        await db.close()
    return (f"skill request #{row['id']} filed: {row['source']} — pending. The operator "
            "fetches and reads it before anything is installed; if approved it appears "
            "in your tools as an installed skill in a later turn.")
