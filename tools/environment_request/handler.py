"""environment_request: file a pending request for a new image variant
(host-side, brokered). Validation and storage live in backend/environments.py."""
from backend import environments, runtime
from backend.agent.tools.toolctx import active_slug
from backend.config import settings
from backend.db import get_db
from backend.packages import PackageError


async def run(name: str = "", packages: list | None = None, reason: str = "",
              min_mem_mb: int | None = None, **kw) -> str:
    if not settings.vm_boxes_enabled:
        return "error: environment requests need boxes (vm_boxes_enabled is off)."
    from_variant = kw.get("from") or kw.get("from_variant") or "main"
    slug = await active_slug()
    db = await get_db()
    try:
        row = await environments.file_request(
            db, project=slug, conversation_id=runtime.conversation_id.get(),
            name=name, from_variant=from_variant, packages=packages or [],
            min_mem_mb=min_mem_mb, reason=reason)
    except PackageError as e:
        return f"error: {e}"
    finally:
        await db.close()
    n = len(row["packages"])
    return (f"environment request #{row['id']} filed: image `{row['name']}` from "
            f"`{row['from_variant']}` with {n} package{'s' if n != 1 else ''} — "
            "pending. Nothing is built until the operator approves it; this "
            "project's boxes then boot on it (a later box boot, not this turn).")
