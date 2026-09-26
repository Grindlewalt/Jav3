"""Operator API for the package catalogue and image variants (WP5).

Cookie-only (require_user) and same-origin gated like every control-plane
route. Nothing here is reachable by an agent: the agent's only way in is the
`package_request` tool, which can only file a pending row.

    GET  /api/packages?status=
    POST /api/packages                      operator "add more" (source=operator)
    POST /api/packages/resolve              dry-run every unresolved pending row
    POST /api/packages/{id}/approve         {acknowledge:true, target_variant}
    POST /api/packages/{id}/reject          {reason}
    POST /api/packages/{id}/remove
    GET  /api/vm/images
    POST /api/vm/images                     {name, from, packages:[...]}
    POST /api/vm/images/{variant}/build     {confirm:true}
    GET  /api/vm/images/{variant}/dockerfile   (WP8's recipe -> Dockerfile)
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import packages
from .auth import require_user
from .config import settings
from .db import get_db
from .vm import images

router = APIRouter(prefix="/api/packages", tags=["packages"],
                   dependencies=[Depends(require_user)])
images_router = APIRouter(prefix="/api/vm/images", tags=["vm-images"],
                          dependencies=[Depends(require_user)])


class PkgRow(BaseModel):
    manager: str
    package: str
    version: str | None = None


class OperatorAdd(BaseModel):
    """One package (manager/package/version) or several (`packages`), a
    reason, and either an existing `target_variant` or a `new_variant` name
    (created FROM `from`, default main)."""
    manager: str | None = None
    package: str | None = None
    version: str | None = None
    packages: list[PkgRow] | None = None
    reason: str
    target_variant: str | None = "main"
    new_variant: str | None = None
    from_: str | None = Field(default="main", alias="from")

    model_config = {"populate_by_name": True}


class ApproveBody(BaseModel):
    acknowledge: bool = False
    target_variant: str | None = None
    build: bool = True


class RejectBody(BaseModel):
    reason: str = ""


class NewVariant(BaseModel):
    name: str
    from_: str | None = Field(default="main", alias="from")
    packages: list[dict] = []

    model_config = {"populate_by_name": True}


class BuildBody(BaseModel):
    confirm: bool = False


def _start_build(variant: str) -> None:
    async def go():
        try:
            await images.builder.build(variant)
        except Exception:  # noqa: BLE001 — the version row / security event carry the error
            pass
    asyncio.get_running_loop().create_task(go())


@router.get("")
async def list_packages(status: str | None = None):
    db = await get_db()
    try:
        try:
            return {"packages": await packages.list_rows(db, status)}
        except packages.PackageError as e:
            raise HTTPException(400, str(e))
    finally:
        await db.close()


@router.post("")
async def operator_add(body: OperatorAdd):
    """Operator "add more": catalogue rows with source=operator (pending until
    the dry-run pins them and the operator approves; approval builds a NEW
    variant version). Single form returns the row; list form
    {packages:[rows], skipped:[{package, error}], target_variant}."""
    rows_in = body.packages if body.packages is not None else (
        [PkgRow(manager=body.manager or "", package=body.package or "",
                version=body.version)])
    if not rows_in or len(rows_in) > 50:
        raise HTTPException(400, "give 1..50 packages")
    try:
        for r in rows_in:                    # validate everything before writing
            packages.validate_request(r.manager, r.package, r.version, None, body.reason)
        target = images.check_variant_name(body.new_variant or body.target_variant or "main")
    except (packages.PackageError, images.RecipeError) as e:
        raise HTTPException(400, str(e))
    db = await get_db()
    filed, skipped = [], []
    try:
        try:
            if body.new_variant:
                await images.create_variant(db, target, body.from_, [])
            elif not await images.variant_exists(db, target):
                raise images.RecipeError(f"no variant `{target}`")
        except images.RecipeError as e:
            raise HTTPException(400, str(e))
        for r in rows_in:
            try:
                filed.append(await packages.file_request(
                    db, manager=r.manager, package=r.package, version=r.version,
                    install_command=None, reason=body.reason, project=None,
                    conversation_id=None, source="operator", target_variant=target))
            except packages.PackageError as e:
                skipped.append({"package": r.package, "error": str(e)})
    finally:
        await db.close()
    if filed:
        images.builder.kick_resolve()
    if body.packages is None:
        if not filed:
            raise HTTPException(400, skipped[0]["error"])
        return filed[0]
    return {"packages": filed, "skipped": skipped, "target_variant": target}


@router.post("/resolve")
async def resolve():
    if not settings.vm_boxes_enabled:
        raise HTTPException(409, "boxes are disabled (vm_boxes_enabled)")
    if images.builder.lock.locked():
        raise HTTPException(409, "the image builder is busy")
    return await images.builder.resolve_pending()


@router.post("/{pkg_id}/approve")
async def approve(pkg_id: int, body: ApproveBody):
    if not body.acknowledge:
        raise HTTPException(400, "approval needs acknowledge: true")
    db = await get_db()
    try:
        try:
            row = await packages.approve(db, pkg_id, target_variant=body.target_variant)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except (packages.PackageError, images.RecipeError) as e:
            raise HTTPException(409, str(e))
        used = await packages.variant_used_by(db, row["target_variant"])
    finally:
        await db.close()
    if body.build and settings.vm_boxes_enabled:
        _start_build(row["target_variant"])
    return {**row, "variant_used_by": used["all"],
            "card": packages.approval_card(row, used),
            "build_started": bool(body.build and settings.vm_boxes_enabled)}


@router.post("/{pkg_id}/reject")
async def reject(pkg_id: int, body: RejectBody):
    db = await get_db()
    try:
        try:
            return await packages.reject(db, pkg_id, reason=body.reason)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except packages.PackageError as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()


@router.post("/{pkg_id}/remove")
async def remove(pkg_id: int):
    db = await get_db()
    try:
        try:
            return await packages.remove(db, pkg_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except packages.PackageError as e:
            raise HTTPException(409, str(e))
    finally:
        await db.close()


@images_router.get("")
async def list_images():
    db = await get_db()
    try:
        return await images.list_images(db)
    finally:
        await db.close()


@images_router.post("")
async def create_variant(body: NewVariant):
    db = await get_db()
    try:
        try:
            info = await images.create_variant(db, body.name, body.from_, body.packages)
        except images.RecipeError as e:
            raise HTTPException(400, str(e))
    finally:
        await db.close()
    return {"name": body.name, "from": info["from"], "recipe_sha256": info["sha"],
            "recipe": images.render_recipe({**info["own"], "packages": info["effective"]})}


@images_router.post("/{variant}/build")
async def build(variant: str, body: BuildBody):
    if not body.confirm:
        raise HTTPException(400, "a build needs confirm: true")
    if not settings.vm_boxes_enabled:
        raise HTTPException(409, "boxes are disabled (vm_boxes_enabled)")
    try:
        images.check_variant_name(variant)
    except images.RecipeError as e:
        raise HTTPException(400, str(e))
    db = await get_db()
    try:
        if not await images.variant_exists(db, variant):
            raise HTTPException(404, f"no variant `{variant}`")
    finally:
        await db.close()
    if images.builder.lock.locked():
        raise HTTPException(409, "the image builder is busy")
    _start_build(variant)
    return {"started": True, "variant": variant}


@images_router.get("/{variant}/dockerfile")
async def dockerfile(variant: str):
    db = await get_db()
    try:
        info = (await images.sync_variants(db)).get(variant)
    finally:
        await db.close()
    if info is None:
        raise HTTPException(404, f"no variant `{variant}`")
    return {"variant": variant, "recipe_sha256": info["sha"],
            "dockerfile": images.dockerfile(variant, info["effective"], sha256=info["sha"])}
