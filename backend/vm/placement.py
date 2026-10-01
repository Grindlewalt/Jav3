"""Per-project placement: which box a project's turns run in ("Runs in").

Before this, the box came from the project's security profile alone
(`separate_box`, `box_runtime`, `box_image`, `box_mem_mb`). Those fields stay
and are now only the DEFAULT for projects that have not chosen. A project's own
setting (table `project_placement`) wins:

    profile  no choice of its own: the profile decides (as before)
    shared   the shared box, whatever the profile says
    own      the project's own box p-<slug>; runtime / image / mem_mb override
             the profile's (NULL = the profile's value). "Separate: new box
             from the same image" is this, with the picked box's image+runtime
    join     another project's existing box (box_id p-<owner>)

Join, and why it is operator-only
---------------------------------
Two projects in one box share its filesystem and processes: either one can
read the other's workspace copy, its running processes and the per-turn op
tokens shipped into the guest. So only the operator (a password session,
`require_user`) may set it, every join raises a `box_joined` security event
(never auto-handled), and the UI shows a one-line warning.

What stays per project in a joined box:

* model_call / tool_broker_call: already per TURN. The gateway resolves the
  envelope (and so the project pin) from the op_id + per-turn token, and the
  box binding only says the op must arrive from the box it was bound to.
* egress policy, secrets, LAN access and attribution: a project box's proxy
  listener used to attribute every connection to `box.project` (the owner).
  In a joined box that would police the joiner's traffic under the OWNER's
  profile and inject the owner's secrets into it. So once a second project
  has run in a box (`Box.joined`, kept for the box's life because the
  joiner's processes may outlive its turn), the proxy attributes it like the
  shared box: by the host-registered turn bound to that box (egress context
  stack), and traffic with no live turn, or with turns of two projects live,
  is unattributed (the Default profile, no secrets injected).
* To make turn-time attribution exact rather than "most recent wins", a
  joined box runs ONE project's turns at a time: a turn of another project
  waits (`boxes.wait_turn_slot`) and is refused with a clear error after
  `boxes.JOIN_WAIT_SECONDS`. A process one project left running while the
  other's turn is live is attributed to the live turn: the shared box's
  residual #7, now also true of a joined box, and the reason join is
  operator-only.

Changing the placement applies from the project's next turn: while a turn of
the project is bound to a box, new turns of it (nested or concurrent, which
reuse that box's workspace copy) keep going there (boxes.for_project).
"""
from __future__ import annotations

import re

from ..config import settings
from . import boxes

MODES = ("profile", "shared", "own", "join")
MEM_MIN, MEM_MAX = 256, 65536
_VARIANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
JOIN_WARNING = ("Two projects in one box share its files and processes: each can "
                "see what the other leaves there. Egress rules, secrets and "
                "attribution stay per project.")


class PlacementError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def owner_of(box_id: str | None) -> str | None:
    """p-<slug> -> slug; anything else -> None. Pure."""
    if isinstance(box_id, str) and box_id.startswith("p-"):
        slug = box_id[2:]
        return slug if boxes._SLUG_RE.match(slug) else None
    return None


def normalize(body: dict | None, slug: str) -> dict:
    """A PUT body -> the stored setting {mode, runtime, image, mem_mb, box_id}.
    Accepts mode "join:<box_id>" as well as mode "join" + box_id. Joining the
    shared box is mode shared; "joining" the project's own box is mode own.
    Pure: rules that need live state (box exists, owner) are in check_join."""
    body = dict(body or {})
    mode = body.get("mode") or "profile"
    box_id = body.get("box_id")
    if isinstance(mode, str) and mode.startswith("join:"):
        mode, box_id = "join", mode[5:]
    if mode not in MODES:
        raise PlacementError("mode must be profile | shared | own | join:<box>", 422)
    out = {"mode": mode, "runtime": None, "image": None, "mem_mb": None, "box_id": None}
    if mode == "join":
        if box_id == boxes.SHARED_ID:
            out["mode"] = "shared"
            return out
        if box_id == f"p-{slug}":
            out["mode"] = mode = "own"
        elif owner_of(box_id) is None:
            raise PlacementError("only the shared box or another project's own box "
                                 "(p-<project>) can be joined", 422)
        else:
            out["box_id"] = box_id
            return out
    if mode == "own":
        rt, image, mem = body.get("runtime"), body.get("image"), body.get("mem_mb")
        if rt not in (None, "") and rt not in boxes.RUNTIMES:
            raise PlacementError("runtime must be kvm | docker", 422)
        if image not in (None, "") and not _VARIANT_RE.match(str(image)):
            raise PlacementError("image must be an image variant name", 422)
        if mem not in (None, ""):
            try:
                mem = int(mem)
            except (TypeError, ValueError):
                raise PlacementError("mem_mb must be a number", 422) from None
            if not MEM_MIN <= mem <= MEM_MAX:
                raise PlacementError(f"mem_mb must be {MEM_MIN}..{MEM_MAX}", 422)
        out.update(runtime=rt or None, image=image or None, mem_mb=mem or None)
    return out


def resolve(setting: dict | None, profile: dict | None, slug: str) -> dict:
    """Project setting > profile default. Pure. Returns
    {mode: shared|own|join, source: project|profile, box_id, runtime, image,
    mem_mb, owner}. runtime/image/mem_mb are what an own box is allocated
    with (None for shared/join)."""
    s = setting or {"mode": "profile"}
    prof = profile or {}
    p_rt = prof.get("box_runtime") or "kvm"
    p_img = prof.get("box_image") or "main"
    p_mem = prof.get("box_mem_mb")
    blank = {"runtime": None, "image": None, "mem_mb": None, "owner": None}
    own_id = f"p-{slug}"
    mode = s.get("mode") or "profile"
    if mode == "profile":
        if prof.get("separate_box"):
            return {"mode": "own", "source": "profile", "box_id": own_id,
                    "runtime": p_rt, "image": p_img, "mem_mb": p_mem, "owner": slug}
        return {"mode": "shared", "source": "profile", "box_id": boxes.SHARED_ID, **blank}
    if mode == "shared":
        return {"mode": "shared", "source": "project", "box_id": boxes.SHARED_ID, **blank}
    if mode == "join" and s.get("box_id") and s["box_id"] != own_id:
        return {"mode": "join", "source": "project", "box_id": s["box_id"],
                **blank, "owner": owner_of(s["box_id"])}
    return {"mode": "own", "source": "project", "box_id": own_id,
            "runtime": s.get("runtime") or p_rt, "image": s.get("image") or p_img,
            "mem_mb": s.get("mem_mb") or p_mem, "owner": slug}


# --- storage ----------------------------------------------------------------

def _row(r) -> dict:
    return {"mode": r["mode"], "runtime": r["runtime"], "image": r["image"],
            "mem_mb": r["mem_mb"], "box_id": r["box_id"]}


async def get_setting(db, slug: str) -> dict | None:
    async with db.execute("SELECT * FROM project_placement WHERE slug = ?",
                          (slug,)) as cur:
        r = await cur.fetchone()
    return _row(r) if r is not None else None


async def load_setting(slug: str) -> dict | None:
    """The stored setting, or None (no row, or no table/DB yet: an install
    that never migrated behaves exactly as before, the profile decides)."""
    from ..db import get_db
    try:
        db = await get_db()
    except Exception:  # noqa: BLE001 — no DB = no choice made
        return None
    try:
        return await get_setting(db, slug)
    except Exception:  # noqa: BLE001 — table missing on an unmigrated DB
        return None
    finally:
        await db.close()


async def effective(slug: str) -> dict:
    """Where the project's next turn runs (setting > profile default)."""
    prof = await boxes.project_profile(slug)
    return resolve(await load_setting(slug), prof, slug)


# --- the rules a PUT has to pass --------------------------------------------

async def _project_exists(db, slug: str) -> bool:
    async with db.execute("SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
                          (slug,)) as cur:
        return await cur.fetchone() is not None


async def check_join(db, slug: str, box_id: str) -> None:
    """A join target must be another live project's own box that exists now,
    or that its owner's placement would create (own). A box that is itself
    only reachable by joining (a chain) or a service/builder box is refused."""
    owner = owner_of(box_id)
    if owner is None or owner == slug:
        raise PlacementError("pick another project's box", 422)
    if not boxes.enabled():
        raise PlacementError("boxes are off on this server (vm_boxes_enabled): "
                             "every project runs in the shared box", 409)
    if not await _project_exists(db, owner):
        raise PlacementError(f"no project {owner!r}", 404)
    box = boxes.get(box_id)
    if box is not None and box.kind != "project":
        raise PlacementError(f"{box_id} is a {box.kind} box: only a project's "
                             "turn box can be joined", 409)
    o = await effective(owner)
    if box is None and o["mode"] != "own":
        raise PlacementError(f"{box_id} does not exist and {owner} does not run in "
                             "its own box, so there is nothing to join", 409)
    if o["mode"] == "join":
        raise PlacementError(f"{owner} itself runs in {o['box_id']}: join that box "
                             "instead", 409)


def cap_warning(eff: dict) -> str | None:
    """Would allocating this own box hit a cap right now? A warning, not a
    refusal: idle boxes give way at turn start, and the turn is where a real
    shortfall is refused (BoxCapError)."""
    if eff["mode"] != "own" or not boxes.enabled() or boxes.get(eff["box_id"]):
        return None
    mem = int(eff.get("mem_mb") or (settings.docker_box_mem_mb if eff["runtime"] == "docker"
                                    else settings.vm_project_box_mem_mb))
    if eff["runtime"] == "kvm":
        mem = max(mem, boxes.mem_floor(eff["image"] or "main"))
    try:
        boxes.registry._check_caps("project", mem, eff["runtime"] or "kvm")
    except boxes.BoxCapError as e:
        return (f"over a cap right now ({e}); at the next turn an idle project box "
                "gives way, otherwise the turn is refused")
    return None


async def put(db, slug: str, body: dict, *, actor: str = "operator",
              by_operator: bool = False) -> dict:
    """Store a project's placement. Raises PlacementError. Returns
    {setting, effective, warnings}. Every change is a `placement_changed`
    event; a join is also a `box_joined` event. `by_operator` is the operator
    route saying the change is their own click (recorded quietly, no alert);
    explicit, never inferred from `actor`, which is only a label."""
    from .. import security
    if not await _project_exists(db, slug):
        raise PlacementError("no such project", 404)
    new = normalize(body, slug)
    if new["mode"] == "own" and new["runtime"] == "docker" and not settings.docker_enabled:
        raise PlacementError("the docker runtime is off on this server (docker_enabled)", 409)
    if new["mode"] == "join":
        await check_join(db, slug, new["box_id"])
    old = await get_setting(db, slug)
    await db.execute(
        "INSERT INTO project_placement(slug, mode, runtime, image, mem_mb, box_id, set_by, "
        "updated_at) VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(slug) DO UPDATE SET mode=excluded.mode, runtime=excluded.runtime, "
        "image=excluded.image, mem_mb=excluded.mem_mb, box_id=excluded.box_id, "
        "set_by=excluded.set_by, updated_at=excluded.updated_at",
        (slug, new["mode"], new["runtime"], new["image"], new["mem_mb"], new["box_id"],
         actor))
    await db.commit()
    eff = await effective(slug)
    warnings = []
    blank = {"mode": "profile", "runtime": None, "image": None, "mem_mb": None,
             "box_id": None}
    if (old or blank) != new:
        await security.raise_event(
            db, kind="placement_changed", severity="info", project=slug,
            summary=f"project {slug} now runs in {describe(eff)} (set by {actor})",
            detail={"project": slug, "from": old, "to": new, "effective": eff,
                    "actor": actor},
            actor=security.OPERATOR if by_operator else None)
    if new["mode"] == "join" and (old or {}).get("box_id") != new["box_id"]:
        owner = owner_of(new["box_id"])
        await security.raise_event(
            db, kind="box_joined", severity="warn", project=slug,
            summary=f"project {slug} joined {new['box_id']} ({owner}'s box) by "
                    f"{actor}: the two share files and processes",
            detail={"project": slug, "box_id": new["box_id"], "owner": owner,
                    "actor": actor, "warning": JOIN_WARNING},
            actor=security.OPERATOR if by_operator else None)
    if new["mode"] == "join":
        warnings.append(JOIN_WARNING)
    w = cap_warning(eff)
    if w:
        warnings.append(w)
    joiners = await joined_by(db, f"p-{slug}")
    if joiners and eff["mode"] != "own":
        warnings.append(f"{', '.join(joiners)} joined this project's box; they stop "
                        "working once it is gone, until they pick another")
    return {"setting": new, "effective": eff, "warnings": warnings}


async def joined_by(db, box_id: str) -> list[str]:
    try:
        async with db.execute("SELECT slug FROM project_placement WHERE mode = 'join' "
                              "AND box_id = ? ORDER BY slug", (box_id,)) as cur:
            return [r["slug"] for r in await cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def describe(eff: dict) -> str:
    if eff["mode"] == "shared":
        return "the shared box"
    if eff["mode"] == "join":
        return f"{eff['box_id']} (joined, {eff.get('owner')}'s box)"
    rt = "container" if eff.get("runtime") == "docker" else "VM"
    return f"its own {rt} {eff['box_id']} (image {eff.get('image') or 'main'})"


# --- the picker's view --------------------------------------------------------

async def overview(db, slug: str) -> dict:
    """GET /api/projects/{slug}/placement: the setting, the effective result,
    every box a turn can run in (in use first, with the projects using it),
    and the image variants for [+ New box] / "separate from the same image"."""
    from .. import profiles
    if not await _project_exists(db, slug):
        raise PlacementError("no such project", 404)
    setting = await get_setting(db, slug)
    eff = await effective(slug)
    prof = await profiles.for_slug(db, slug)
    async with db.execute("SELECT slug FROM projects WHERE deleted_at IS NULL "
                          "ORDER BY slug") as cur:
        slugs = [r["slug"] for r in await cur.fetchall()]
    used: dict[str, list[str]] = {}
    for s in slugs:
        e = eff if s == slug else await effective(s)
        # boxes off: every turn runs in the shared box whatever is stored
        bid = e["box_id"] if boxes.enabled() else boxes.SHARED_ID
        used.setdefault(bid, []).append(s)
    rows = []
    for b in boxes.all_boxes():
        if b.kind not in ("shared", "project"):
            continue
        ctl = boxes.controller(b) if b.is_shared else b.ctl
        running = bool(ctl and ctl.running())
        users = used.get(b.id, [])
        rows.append({"id": b.id, "kind": b.kind, "owner": b.project,
                     "runtime": b.runtime, "image": b.image[0], "mem_mb": b.mem_mb,
                     "state": "running" if running else "stopped",
                     "used_by": users, "joined": sorted(b.joined),
                     "in_use": running or bool(users), "current": b.id == eff["box_id"]})
    rows.sort(key=lambda r: (not r["in_use"], r["kind"] != "shared", r["id"]))
    images = []
    try:
        from . import images as images_mod
        info = await images_mod.sync_variants(db)
        for name in sorted(info):
            images.append({"name": name, "version": boxes.image_version(name),
                           "min_mem_mb": info[name].get("min_mem_mb")})
    except Exception:  # noqa: BLE001 — the picker still works with main alone
        images = [{"name": "main", "version": None, "min_mem_mb": None}]
    return {"slug": slug, "enabled": boxes.enabled(),
            "docker_enabled": bool(settings.docker_enabled),
            "setting": setting or {"mode": "profile", "runtime": None, "image": None,
                                   "mem_mb": None, "box_id": None},
            "effective": eff, "described": describe(eff),
            "profile": {"id": prof.get("id"), "name": prof.get("name"),
                        "separate_box": bool(prof.get("separate_box")),
                        "box_runtime": prof.get("box_runtime") or "kvm",
                        "box_image": prof.get("box_image") or "main",
                        "box_mem_mb": prof.get("box_mem_mb")},
            "boxes": rows, "images": images, "budget": boxes.budget(),
            "join_warning": JOIN_WARNING}
