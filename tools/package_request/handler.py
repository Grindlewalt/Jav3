"""package_request: file a persistent package request (host-side, brokered).

Validation, the canonical command and the catalogue row all live in
backend/packages.py. The agent's install_command is stored, never run."""
from backend import packages, runtime
from backend.agent.tools.toolctx import active_slug
from backend.config import settings
from backend.db import get_db


async def run(manager: str = "", package: str = "", install_command: str = "",
              reason: str = "", version: str | None = None) -> str:
    if not settings.vm_boxes_enabled:
        return "error: package requests need boxes (vm_boxes_enabled is off)."
    slug = await active_slug()
    db = await get_db()
    try:
        row = await packages.file_request(
            db, manager=manager, package=package, version=version,
            install_command=install_command, reason=reason, project=slug,
            conversation_id=runtime.conversation_id.get(), source="agent")
    except packages.PackageError as e:
        return f"error: {e}"
    finally:
        await db.close()
    try:
        from backend.vm import images
        images.builder.kick_resolve()
    except Exception:  # noqa: BLE001 — resolution is retried from the review card
        pass
    pin = f" {row['version_req']}" if row["version_req"] else ""
    return (f"package request #{row['id']} filed: {row['manager']} {row['package']}{pin} "
            f"for image `{row['target_variant']}` — status pending. Nothing is "
            "installed until the operator approves it; it then arrives with a new "
            "image version on a later box boot, not in this turn.")
