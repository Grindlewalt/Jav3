"""Image variants: recipes, frozen layers, the builder box (DESIGN-BOXES 2(e), WP5).

A *variant* is what a box boots: `base-vN <- layer-<variant>-vM (ro, frozen)
<- per-boot overlay`. The layer is produced by a **builder box** (boxes kind
"builder"): it boots on its own tap, reaches the network only through its own
proxy listener (attributed to `__image_build__`, a registry-only profile: WP2),
fetches this module's builder package over vsock, installs the recipe, records
`baseline.json`, reports, and powers off. Its overlay is then frozen as the
layer. A running image is never mutated: every change is a NEW version.

Recipe format (vm/images/<name>.recipe; one recipe feeds BOTH the KVM layer
and the hardened Docker image WP8 builds, via `kvm_steps` / `dockerfile`):

    # comment
    name dev                 # [a-z0-9][a-z0-9_-]{0,31}
    from main                # parent variant, or `none`
    min_mem_mb 768           # optional; the box-memory floor for this variant
    apt golang rustc=1.85.0+dfsg1-1     # manager, then one or more packages
    pip uv==0.8.3            # pin: apt name=ver, pip name==ver, npm name@ver
    npm @scope/pkg@1.2.3 typescript

Only package lines exist: there is no free-form RUN. Every package token goes
through the same validator as an agent's `package_request`
(backend/packages.py), and the commands are built from the validated fields.

Semantics:
  * effective(V) = packages of V's `from` chain + V's own + the catalogue rows
    approved for V (status approved/building/built/failed).
  * `main` is the golden base: vm/build_base.sh bakes main.recipe's apt list.
    A KVM layer installs effective(V) - main.recipe (can only ADD to the base),
    so `main` and `svc` need no layer until something is approved for them.
  * Docker (WP8): `dockerfile(V)` = debian:trixie-slim + effective(V), exec-form
    RUN lines only, same canonical commands.
  * Hash: sha256 over the canonical JSON of (name, min_mem_mb, sorted
    effective packages). The row keeps it; a version records the hash it was
    built from, so "needs a build" is a string compare.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from ..packages import (PackageError, canonical_argv, validate_name,
                        validate_version)
from . import boxes

RECIPE_DIR_NAME = ("vm", "images")
BASE_VARIANT = "main"
DOCKER_BASE = "debian:trixie-slim"
PIP_VENV = "/opt/jav3/py"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_LAYER_RE = re.compile(r"^layer-([a-z0-9][a-z0-9_-]{0,31})-v(\d+)\.qcow2$")
BUS_CHAN = "vm-images"
BUILD_USER = "__image_build__"     # the egress attribution of every builder box


class RecipeError(ValueError):
    pass


# --- recipes (pure) ---------------------------------------------------------

def check_variant_name(name: str | None) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise RecipeError(f"bad variant name {name!r}")
    return name


def _split_pin(manager: str, token: str) -> tuple[str, str | None]:
    if manager == "apt" and "=" in token:
        n, _, v = token.partition("=")
        return n, v
    if manager == "pip" and "==" in token:
        n, _, v = token.partition("==")
        return n, v
    if manager == "npm" and "@" in token[1:]:
        i = token.rindex("@")
        return token[:i], token[i + 1:]
    return token, None


def package_entry(manager: str, package: str, version: str | None = None) -> dict:
    """One validated recipe package. Raises RecipeError."""
    try:
        return {"manager": manager, "package": validate_name(manager, package),
                "version": validate_version(manager, version)}
    except PackageError as e:
        raise RecipeError(str(e)) from None


def parse_recipe(text: str, *, source: str = "<recipe>") -> dict:
    """Recipe text -> {"name", "from", "min_mem_mb", "packages": [...]}."""
    out: dict = {"name": None, "from": BASE_VARIANT, "min_mem_mb": None, "packages": []}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, *rest = line.split()
        where = f"{source}:{n}"
        if key == "name":
            if len(rest) != 1:
                raise RecipeError(f"{where}: name takes one value")
            out["name"] = check_variant_name(rest[0])
        elif key == "from":
            if len(rest) != 1:
                raise RecipeError(f"{where}: from takes one value")
            out["from"] = None if rest[0] == "none" else check_variant_name(rest[0])
        elif key == "min_mem_mb":
            if len(rest) != 1 or not rest[0].isdigit() or not 128 <= int(rest[0]) <= 16384:
                raise RecipeError(f"{where}: min_mem_mb takes one integer (128..16384)")
            out["min_mem_mb"] = int(rest[0])
        elif key in ("apt", "pip", "npm"):
            if not rest:
                raise RecipeError(f"{where}: {key} needs at least one package")
            for tok in rest:
                name, ver = _split_pin(key, tok)
                try:
                    out["packages"].append(package_entry(key, name, ver))
                except RecipeError as e:
                    raise RecipeError(f"{where}: {e}") from None
        else:
            raise RecipeError(f"{where}: unknown directive {key!r} "
                              "(name, from, min_mem_mb, apt, pip, npm)")
    if not out["name"]:
        raise RecipeError(f"{source}: missing `name`")
    if out["from"] == out["name"]:
        raise RecipeError(f"{source}: a variant cannot be built from itself")
    out["packages"] = _dedupe(out["packages"])
    return out


def _pin(p: dict) -> str:
    v = p.get("version")
    if not v:
        return p["package"]
    return {"apt": f"{p['package']}={v}", "pip": f"{p['package']}=={v}",
            "npm": f"{p['package']}@{v}"}[p["manager"]]


def render_recipe(r: dict) -> str:
    lines = [f"name {r['name']}", f"from {r.get('from') or 'none'}"]
    if r.get("min_mem_mb"):
        lines.append(f"min_mem_mb {r['min_mem_mb']}")
    for m in ("apt", "pip", "npm"):
        toks = [_pin(p) for p in r.get("packages", []) if p["manager"] == m]
        if toks:
            lines.append(f"{m} " + " ".join(toks))
    return "\n".join(lines) + "\n"


def _key(p: dict) -> tuple:
    return (p["manager"], p["package"])


def _dedupe(pkgs: list[dict]) -> list[dict]:
    """One entry per (manager, package); a later pinned entry wins."""
    out: dict[tuple, dict] = {}
    for p in pkgs:
        k = _key(p)
        if k not in out or p.get("version"):
            out[k] = {"manager": p["manager"], "package": p["package"],
                      "version": p.get("version")}
    return sorted(out.values(), key=lambda p: (("apt", "pip", "npm").index(p["manager"]),
                                                p["package"]))


def recipe_sha256(name: str, packages: list[dict], min_mem_mb: int | None) -> str:
    canon = json.dumps({"name": name, "min_mem_mb": min_mem_mb,
                        "packages": _dedupe(packages)},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def recipe_dir() -> Path:
    return settings.base_dir.joinpath(*RECIPE_DIR_NAME)


def builtin_recipes() -> dict[str, dict]:
    out = {}
    for p in sorted(recipe_dir().glob("*.recipe")):
        r = parse_recipe(p.read_text(), source=p.name)
        if r["name"] != p.stem:
            raise RecipeError(f"{p.name}: name {r['name']!r} != file name")
        out[r["name"]] = r
    return out


def effective_packages(name: str, recipes: dict[str, dict],
                       extras: dict[str, list[dict]] | None = None) -> list[dict]:
    """The full package set of variant `name`: its `from` chain, its own
    recipe, and the approved catalogue extras of every variant on the chain.
    Pure; RecipeError on an unknown parent or a cycle."""
    extras = extras or {}
    chain, cur = [], name
    while cur is not None:
        if cur in chain:
            raise RecipeError(f"variant cycle: {' -> '.join(chain + [cur])}")
        if cur not in recipes:
            raise RecipeError(f"unknown variant {cur!r}")
        chain.append(cur)
        cur = recipes[cur].get("from")
    pkgs: list[dict] = []
    for v in reversed(chain):                 # root first; children override
        pkgs += recipes[v].get("packages", [])
        pkgs += extras.get(v, [])
    return _dedupe(pkgs)


def layer_packages(effective: list[dict], base: list[dict]) -> list[dict]:
    """What a KVM layer must install on top of base-vN. A pinned entry whose
    pin differs from the base's is installed (it changes the version)."""
    have = {_key(p): p.get("version") for p in base}
    return [p for p in effective
            if _key(p) not in have or (p.get("version") and p.get("version") != have[_key(p)])]


def kvm_steps(pkgs: list[dict]) -> list[dict]:
    """The builder's install plan: [{"kind", "argv", "items"}], argv built from
    validated fields by packages.canonical_argv. apt first (in one transaction),
    then the pip venv, then npm. Pure."""
    steps: list[dict] = []
    apt = [p for p in pkgs if p["manager"] == "apt"]
    pip = [p for p in pkgs if p["manager"] == "pip"]
    npm = [p for p in pkgs if p["manager"] == "npm"]
    if apt:
        argv = canonical_argv("apt", apt[0]["package"], apt[0].get("version"))[:-1]
        argv += [canonical_argv("apt", p["package"], p.get("version"))[-1] for p in apt]
        steps.append({"kind": "apt", "argv": argv, "items": apt})
    if pip:
        steps.append({"kind": "venv", "argv": ["python3", "-m", "venv", PIP_VENV],
                      "items": []})
        argv = canonical_argv("pip", pip[0]["package"], pip[0].get("version"))[:-1]
        argv += [canonical_argv("pip", p["package"], p.get("version"))[-1] for p in pip]
        steps.append({"kind": "pip", "argv": argv, "items": pip})
    if npm:
        argv = canonical_argv("npm", npm[0]["package"], npm[0].get("version"))[:-1]
        argv += [canonical_argv("npm", p["package"], p.get("version"))[-1] for p in npm]
        steps.append({"kind": "npm", "argv": argv, "items": npm})
    return steps


def dockerfile(name: str, pkgs: list[dict], *, sha256: str = "",
               base_image: str = DOCKER_BASE) -> str:
    """The hardened Docker image for a variant (WP8 builds it). Exec-form RUN
    only (no shell parses any package token), --no-install-recommends, apt
    lists removed, pip into the /opt/jav3/py venv, npm into /usr/local, and a
    non-root user the container runs as. python3 is always present (the guest
    runtime is Python). Pure."""
    apt = _dedupe([{"manager": "apt", "package": "python3", "version": None},
                   {"manager": "apt", "package": "ca-certificates", "version": None},
                   *[p for p in pkgs if p["manager"] == "apt"]])
    rest = [p for p in pkgs if p["manager"] != "apt"]
    if any(p["manager"] == "pip" for p in rest):
        apt = _dedupe(apt + [{"manager": "apt", "package": "python3-venv", "version": None}])
    if any(p["manager"] == "npm" for p in rest):
        apt = _dedupe(apt + [{"manager": "apt", "package": "npm", "version": None}])
    j = lambda argv: json.dumps(argv)   # noqa: E731
    lines = [f"# generated by backend/vm/images.py from vm/images/{name}.recipe; do not edit",
             f"FROM {base_image}",
             f'LABEL jav3.variant="{name}" jav3.recipe_sha256="{sha256}"',
             "ENV DEBIAN_FRONTEND=noninteractive "
             f"PATH={PIP_VENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             f"RUN {j(['apt-get', 'update'])}"]
    for st in kvm_steps(apt + rest):
        lines.append(f"RUN {j(st['argv'])}")
    lines += [f"RUN {j(['rm', '-rf', '/var/lib/apt/lists', '/root/.cache', '/root/.npm'])}",
              f"RUN {j(['useradd', '--create-home', '--uid', '10001', 'jav3'])}",
              "USER 10001:10001"]
    return "\n".join(lines) + "\n"


# --- the DB mirror of variants ------------------------------------------------

def _db_sync() -> sqlite3.Connection:
    c = sqlite3.connect(str(settings.db_path), timeout=5)
    c.row_factory = sqlite3.Row
    return c


async def sync_variants(db) -> dict[str, dict]:
    """Make image_variants match the builtin files + operator rows + the
    approved catalogue, recomputing each effective recipe and its hash.
    Returns {name: {own, effective, layer, sha, min_mem_mb, from, builtin}}."""
    from .. import packages
    recipes = builtin_recipes()
    builtin = set(recipes)
    async with db.execute("SELECT name, recipe, from_variant FROM image_variants "
                          "WHERE builtin = 0") as cur:
        for r in await cur.fetchall():
            try:
                own = json.loads(r["recipe"] or "{}").get("own")
            except ValueError:
                own = None
            if own and r["name"] not in builtin:
                recipes[r["name"]] = own
    extras: dict[str, list[dict]] = {}
    for name in recipes:
        extras[name] = [{"manager": p["manager"], "package": p["package"],
                         "version": p["resolved_version"] or p["version_req"]}
                        for p in await packages.approved_for(db, name)]
    # the base set is always the CHECKED-IN main recipe (what build_base baked)
    base = _dedupe(builtin_recipes()[BASE_VARIANT]["packages"])
    out = {}
    for name, own in recipes.items():
        try:
            eff = effective_packages(name, recipes, extras)
        except RecipeError:
            continue
        mem = own.get("min_mem_mb")
        par = own.get("from")
        while mem is None and par:            # a child inherits its parent's floor
            mem = recipes.get(par, {}).get("min_mem_mb")
            par = recipes.get(par, {}).get("from")
        if name == "desktop" or _descends(name, "desktop", recipes):
            mem = max(mem or 0, settings.vm_desktop_min_mem_mb)
        sha = recipe_sha256(name, eff, mem)
        info = {"own": own, "effective": eff, "layer": layer_packages(eff, base),
                "sha": sha, "min_mem_mb": mem, "from": own.get("from"),
                "builtin": name in builtin}
        out[name] = info
        await db.execute(
            "INSERT INTO image_variants(name, from_variant, builtin, recipe, "
            "recipe_sha256, min_mem_mb) VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO "
            "UPDATE SET from_variant = excluded.from_variant, builtin = excluded.builtin, "
            "recipe = excluded.recipe, recipe_sha256 = excluded.recipe_sha256, "
            "min_mem_mb = excluded.min_mem_mb, updated_at = datetime('now')",
            (name, own.get("from"), int(name in builtin),
             json.dumps({"own": own, "effective": eff, "layer": info["layer"]}),
             sha, mem))
    await db.commit()
    return out


def _descends(name: str, ancestor: str, recipes: dict) -> bool:
    cur, seen = recipes.get(name, {}).get("from"), set()
    while cur and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        cur = recipes.get(cur, {}).get("from")
    return False


async def variant_exists(db, name: str) -> bool:
    if name in builtin_recipes():
        return True
    async with db.execute("SELECT 1 FROM image_variants WHERE name = ?", (name,)) as cur:
        return await cur.fetchone() is not None


async def descendants(db, name: str) -> set[str]:
    """Variants built FROM `name`, transitively (they inherit its packages)."""
    parents: dict[str, str | None] = {n: r.get("from") for n, r in builtin_recipes().items()}
    try:
        async with db.execute("SELECT name, from_variant FROM image_variants") as cur:
            for r in await cur.fetchall():
                parents.setdefault(r[0], r[1])
    except sqlite3.OperationalError:
        pass
    out = set()
    for n in parents:
        cur, seen = parents.get(n), set()
        while cur and cur not in seen:
            if cur == name:
                out.add(n)
                break
            seen.add(cur)
            cur = parents.get(cur)
    return out


async def create_variant(db, name: str, from_variant: str | None,
                         packages_in: list[dict]) -> dict:
    """An operator variant (POST /api/vm/images). Its own recipe is stored on
    its row; it is not built until the operator asks."""
    name = check_variant_name(name)
    if name in builtin_recipes():
        raise RecipeError(f"`{name}` is a builtin variant")
    if await variant_exists(db, name):
        raise RecipeError(f"variant `{name}` exists")
    if from_variant is not None:
        check_variant_name(from_variant)
        if not await variant_exists(db, from_variant):
            raise RecipeError(f"no variant `{from_variant}`")
    if not isinstance(packages_in, list) or len(packages_in) > 100:
        raise RecipeError("packages must be a list of at most 100")
    pkgs = _dedupe([package_entry(str(p.get("manager")), p.get("package"),
                                  p.get("version")) for p in packages_in])
    own = {"name": name, "from": from_variant, "min_mem_mb": None, "packages": pkgs}
    await db.execute("INSERT INTO image_variants(name, from_variant, builtin, recipe) "
                     "VALUES (?,?,0,?)", (name, from_variant, json.dumps({"own": own})))
    await db.commit()
    return (await sync_variants(db))[name]


def min_mem_mb(variant: str) -> int | None:
    """The memory floor of a variant (desktop and anything built from it:
    >= vm_desktop_min_mem_mb). Sync, for boxes' allocation (WP1 hook)."""
    try:
        with _db_sync() as c:
            r = c.execute("SELECT min_mem_mb FROM image_variants WHERE name = ?",
                          (variant,)).fetchone()
        if r is not None and r[0]:
            return int(r[0])
    except sqlite3.Error:
        pass
    try:
        rec = builtin_recipes().get(variant)
    except RecipeError:
        rec = None
    return rec.get("min_mem_mb") if rec else None


# --- versions + the boxes image resolver ---------------------------------------

def layer_path(variant: str, version: int) -> Path:
    return settings.vm_dir / f"layer-{variant}-v{version}.qcow2"


def baseline_file(image: Path) -> Path:
    """<image>.baseline.json beside a base-vN / layer qcow2."""
    name = image.name[:-len(".qcow2")] if image.name.endswith(".qcow2") else image.name
    return image.with_name(name + ".baseline.json")


def _base() -> Path:
    from .lifecycle import _base_image
    return _base_image()


def resolve_image(box) -> Path | None:
    """boxes.add_image_resolver hook: the qcow2 a box's overlay backs on.

    builder -> the active base (a layer is built on the base, never on a layer)
    variant with a built, active version (or the version the box pins) -> its
      layer; a version whose layer is empty -> the base it was recorded on
    variant never built whose layer is empty (main, svc) -> the base
    otherwise None (boxes then refuses: 'not built')."""
    if box.kind == "builder":
        return _base()
    variant, version = box.image
    try:
        with _db_sync() as c:
            q = ("SELECT version, path, base_version FROM image_versions WHERE variant = ? "
                 "AND status = 'built' ")
            if version and str(version).lstrip("v").isdigit():
                row = c.execute(q + "AND version = ?", (variant, int(str(version).lstrip("v")))).fetchone()
            else:
                row = c.execute(q + "AND active = 1 ORDER BY version DESC LIMIT 1",
                                (variant,)).fetchone()
            vrow = c.execute("SELECT recipe FROM image_variants WHERE name = ?",
                             (variant,)).fetchone()
    except sqlite3.Error:
        row = vrow = None
    if row is not None:
        if row["path"]:
            p = Path(row["path"])
            return p if p.exists() else None
        b = settings.vm_dir / f"base-{row['base_version']}.qcow2"
        return b if b.exists() else _base()
    layer = None
    if vrow is not None:
        try:
            layer = json.loads(vrow[0] or "{}").get("layer")
        except ValueError:
            layer = None
    if layer is None:
        try:
            recipes = builtin_recipes()
            if variant in recipes:
                layer = layer_packages(effective_packages(variant, recipes),
                                       _dedupe(recipes[BASE_VARIANT]["packages"]))
        except RecipeError:
            layer = None
    if layer == []:
        return _base()
    return None


def referenced_bases() -> set[str]:
    """base-vN names a live layer still backs on (keep those bases)."""
    try:
        with _db_sync() as c:
            rows = c.execute("SELECT DISTINCT base_version FROM image_versions "
                             "WHERE status = 'built' AND path IS NOT NULL").fetchall()
        return {f"base-{r[0]}.qcow2" for r in rows}
    except sqlite3.Error:
        return set()


def baseline_for(box) -> dict | None:
    """The baseline.json of the image a box runs (WP4's expected-process and
    package baseline). None when not recorded."""
    try:
        p = boxes.image_path(box)
    except Exception:  # noqa: BLE001
        return None
    b = baseline_file(p)
    if not b.exists():
        return None
    try:
        return json.loads(b.read_text())
    except (OSError, ValueError):
        return None


# --- the builder ---------------------------------------------------------------

@dataclass
class Job:
    mode: str                                 # "build" | "resolve"
    variant: str
    version: int | None = None
    steps: list = field(default_factory=list)
    items: list = field(default_factory=list)  # resolve / verify items
    catalogue_ids: list = field(default_factory=list)
    sha: str = ""
    box_id: str | None = None
    token: str = field(default_factory=lambda: os.urandom(16).hex())
    future: asyncio.Future | None = None
    log: list = field(default_factory=list)
    started: float = field(default_factory=time.time)


class Builder:
    """One builder at a time (the Pi has room for one). State for the API."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None
        self.phase: str | None = None
        self._resolve_pending = False

    def state(self) -> dict:
        j = self.current
        return {"running": j is not None, "variant": j.variant if j else None,
                "mode": j.mode if j else None, "phase": self.phase,
                "log_tail": (j.log[-20:] if j else [])}

    def _pub(self, **ev) -> None:
        from .. import bus
        bus.publish(BUS_CHAN, {"type": "image_build", **ev})

    # the package the builder box fetches (gateway get_guest_package)
    def package(self, box) -> bytes:
        job = self.jobs.get(box.id)
        if job is None:
            raise RuntimeError(f"no build job for {box.id}")
        runner = (Path(__file__).with_name("builder_guest.py")).read_bytes()
        spec = {"v": 1, "mode": job.mode, "token": job.token, "variant": job.variant,
                "version": job.version, "steps": job.steps, "items": job.items,
                "pip_venv": PIP_VENV}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for arc, data in (("backend/__init__.py", b""),
                              ("backend/server.py", runner),
                              ("backend/job.json", json.dumps(spec).encode())):
                ti = tarfile.TarInfo(arc)
                ti.size, ti.mode = len(data), 0o644
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    # gateway op build_report (from a builder box only; the gateway gates kind)
    async def on_report(self, loop, conn, req: dict, box) -> None:
        job = self.jobs.get(getattr(box, "id", None))
        reply = {"type": "build_ack", "ok": True}
        if job is None or req.get("token") != job.token:
            reply = {"type": "error", "error": "unknown_job"}
        elif req.get("phase") == "log":
            line = str(req.get("line", ""))[:400]
            job.log.append(line)
            del job.log[:-200]
            self._pub(phase="log", variant=job.variant, line=line)
        elif req.get("phase") == "result" and job.future and not job.future.done():
            job.future.set_result(req)
        try:
            await loop.sock_sendall(conn, (json.dumps(reply) + "\n").encode())
        except OSError:
            pass

    async def _run_box(self, job: Job) -> dict:
        """Boot a builder box for `job`, wait for its result and power-off.
        In build mode the overlay is hard-linked BEFORE the guest writes to it,
        so it survives the controller deleting the box directory."""
        loop = asyncio.get_running_loop()
        job.future = loop.create_future()
        box = boxes.allocate("builder", variant=job.variant, version="build",
                             mem_mb=settings.vm_builder_box_mem_mb, runtime="kvm")
        job.box_id = box.id
        self.jobs[box.id] = job
        part = None
        try:
            ctl = boxes.controller(box)
            self.phase = "boot"
            self._pub(phase="boot", variant=job.variant, box=box.id)
            await ctl.boot()
            if job.mode == "build":
                overlay = box.dir / "overlay.qcow2"
                for _ in range(100):
                    if overlay.exists():
                        break
                    await asyncio.sleep(0.1)
                part = layer_path(job.variant, job.version).with_suffix(".qcow2.part")
                part.unlink(missing_ok=True)
                os.link(overlay, part)
            self.phase = "run"
            report = await asyncio.wait_for(job.future, settings.vm_builder_timeout_seconds)
            self.phase = "poweroff"
            clean = False
            for _ in range(240):
                if not ctl.running():
                    clean = True
                    break
                await asyncio.sleep(0.5)
            report = dict(report)
            report["_clean_poweroff"] = clean
            report["_part"] = str(part) if part else None
            return report
        except BaseException:
            if part is not None:
                part.unlink(missing_ok=True)
            raise
        finally:
            self.jobs.pop(box.id, None)
            try:
                await boxes.destroy(box)
            except Exception:  # noqa: BLE001
                boxes.release(box.id)
                shutil.rmtree(box.dir, ignore_errors=True)

    # ---- resolve (dry-run) -------------------------------------------------
    async def resolve_pending(self) -> dict:
        """Dry-run every unresolved pending request in one builder boot."""
        from .. import packages
        from ..db import get_db
        db = await get_db()
        try:
            rows = await packages.unresolved(db)
        finally:
            await db.close()
        if not rows:
            return {"resolved": 0}
        items = [{"id": r["id"], "manager": r["manager"], "package": r["package"],
                  "version": r["version_req"]} for r in rows]
        async with self.lock:
            job = Job(mode="resolve", variant=BASE_VARIANT, items=items)
            self.current = job
            try:
                report = await self._run_box(job)
            except Exception as e:  # noqa: BLE001
                report = {"ok": False, "error": f"{type(e).__name__}: {e}", "results": []}
            finally:
                self.current, self.phase = None, None
        by_id = {r["id"]: r for r in rows}
        db = await get_db()
        try:
            n = 0
            for res in report.get("results") or []:
                if not isinstance(res, dict) or res.get("id") not in by_id:
                    continue
                await packages.set_resolution(
                    db, res["id"], resolved_version=_s(res.get("version"), 80),
                    integrity=_s(res.get("integrity"), 300),
                    error=_s(res.get("error"), 200))
                n += 1
        finally:
            await db.close()
        self._pub(phase="resolved", count=n, error=report.get("error"))
        return {"resolved": n, "error": report.get("error")}

    def kick_resolve(self) -> None:
        """Debounced background resolve after a request is filed."""
        if self._resolve_pending or not settings.vm_boxes_enabled:
            return
        self._resolve_pending = True

        async def later():
            await asyncio.sleep(5)
            self._resolve_pending = False
            try:
                await self.resolve_pending()
            except Exception:  # noqa: BLE001 — resolution can be retried from the API
                pass
        asyncio.get_running_loop().create_task(later())

    # ---- build -------------------------------------------------------------
    async def build(self, variant: str) -> dict:
        """Build the NEXT version of `variant` from its current effective
        recipe. Never touches an existing version."""
        from .. import packages, security
        from ..db import get_db
        check_variant_name(variant)
        from .lifecycle import _active_version
        async with self.lock:
            db = await get_db()
            try:
                info = (await sync_variants(db)).get(variant)
                if info is None:
                    raise RecipeError(f"no variant `{variant}`")
                async with db.execute("SELECT COALESCE(MAX(version), 0) FROM image_versions "
                                      "WHERE variant = ?", (variant,)) as cur:
                    version = (await cur.fetchone())[0] + 1
                base_version = _active_version()
                cat = [r["id"] for r in await packages.approved_for(db, variant)]
                await db.execute(
                    "INSERT INTO image_versions(variant, version, base_version, "
                    "recipe_sha256, status) VALUES (?,?,?,?, 'building')",
                    (variant, version, base_version, info["sha"]))
                await db.commit()
                await packages.set_status(db, cat, "building")
            finally:
                await db.close()
            layer = info["layer"]
            verify = [{"manager": p["manager"], "package": p["package"],
                       "version": p.get("version")} for p in layer]
            integ = await _integrities(cat)
            for it in verify:
                it["integrity"] = integ.get((it["manager"], it["package"], it["version"]))
            job = Job(mode="build", variant=variant, version=version,
                      steps=kvm_steps(layer), items=verify, catalogue_ids=cat,
                      sha=info["sha"])
            self.current = job
            self._pub(phase="start", variant=variant, version=version)
            report, path, size, err = {}, None, None, None
            try:
                if layer:
                    report = await self._run_box(job)
                    err = None if report.get("ok") else (_s_tail(report.get("error"), 2000) or "build failed")
                    if not err and not report.get("_clean_poweroff"):
                        err = "the builder did not power off cleanly; layer discarded"
                    part = Path(report["_part"]) if report.get("_part") else None
                    if err is None and part is not None:
                        path, size = await self._freeze(part, variant, version)
                        bl = _sanitize_baseline(report.get("baseline"))
                        bl.update({"image": path.name, "variant": variant,
                                   "version": version, "base_version": base_version,
                                   "recipe_sha256": info["sha"]})
                        baseline_file(path).write_text(
                            json.dumps(bl, indent=1, sort_keys=True))
                    elif part is not None:
                        part.unlink(missing_ok=True)
                else:
                    report = {"ok": True}          # nothing to add: the base is the image
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            finally:
                self.current, self.phase = None, None
            db = await get_db()
            try:
                status = "failed" if err else "built"
                await db.execute(
                    "UPDATE image_versions SET status = ?, path = ?, size_bytes = ?, "
                    "baseline_path = ?, build_log = ?, built_at = datetime('now') "
                    "WHERE variant = ? AND version = ?",
                    (status, str(path) if path else None, size,
                     str(baseline_file(path)) if path else None,
                     "\n".join(job.log[-200:])[-20000:] + (f"\nERROR: {err}" if err else ""),
                     variant, version))
                if not err:
                    await db.execute("UPDATE image_versions SET active = (version = ?) "
                                     "WHERE variant = ?", (version, variant))
                await db.commit()
                await packages.set_status(db, cat, "failed" if err else "built",
                                          built_version=None if err else version)
                used = await packages.variant_used_by(db, variant)
                await security.raise_event(
                    db, kind="image_variant_built", severity="warn" if err else "info",
                    summary=(f"image variant `{variant}` v{version} "
                             f"{'FAILED: ' + _s_tail(err, 300) if err else 'built'} "
                             f"(used by: {', '.join(used['all']) or 'none'})"),
                    detail={"variant": variant, "version": version, "ok": not err,
                            "base_version": base_version, "recipe_sha256": info["sha"],
                            "packages": [_pin(p) for p in layer], "error": err,
                            "variant_used_by": used["all"], "size_bytes": size})
            finally:
                await db.close()
            self._pub(phase="done", variant=variant, version=version, ok=not err, error=err)
            return {"variant": variant, "version": version, "ok": not err, "error": err}

    async def _freeze(self, part: Path, variant: str, version: int) -> tuple[Path, int]:
        """Point the layer at its base by absolute path (the overlay may have
        named it relative to the box dir), check it, make it read-only."""
        base = _base()
        for argv in (["qemu-img", "rebase", "-u", "-b", str(base), "-F", "qcow2", str(part)],
                     ["qemu-img", "check", str(part)]):
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, e = await proc.communicate()
            if proc.returncode != 0:
                part.unlink(missing_ok=True)
                raise RuntimeError(f"{argv[1]} failed: {e.decode(errors='replace')[:300]}")
        final = layer_path(variant, version)
        os.replace(part, final)
        os.chmod(final, 0o444)
        return final, final.stat().st_blocks * 512


def _s(v, n: int) -> str | None:
    """A guest-supplied string, printable and bounded (the builder ran
    untrusted install scripts as root before it reported)."""
    if not isinstance(v, str):
        return None
    return "".join(ch for ch in v if ch.isprintable())[:n] or None


def _s_tail(v, n: int) -> str | None:
    """_s for an error: apt/dpkg print the root cause LAST, so a long one keeps
    its head (what failed) and its tail (why), not just the head (e2e BUG-9)."""
    t = _s(v, 1 << 20)
    if t is None or len(t) <= n:
        return t
    head = min(200, n // 4)
    return t[:head] + " [...] " + t[-(n - head - 7):]


def _sanitize_baseline(bl) -> dict:
    """baseline.json from the guest: known keys, strings bounded, lists capped."""
    if not isinstance(bl, dict):
        return {"v": 1}
    out: dict = {"v": 1}
    for key in ("dpkg", "pip", "npm"):
        d = bl.get(key)
        if isinstance(d, dict):
            out[key] = {_s(k, 120): _s(v, 80) for k, v in list(d.items())[:5000]
                        if _s(k, 120)}
    for key in ("units_enabled", "setuid", "listening", "processes"):
        d = bl.get(key)
        if isinstance(d, list):
            out[key] = [x for x in (_s(v, 300) for v in d[:2000]) if x]
    out["captured_at"] = _s(bl.get("captured_at"), 40)
    return out


async def _integrities(ids: list[int]) -> dict:
    from ..db import get_db
    if not ids:
        return {}
    db = await get_db()
    try:
        q = ",".join("?" * len(ids))
        async with db.execute(f"SELECT manager, package, resolved_version, integrity "
                              f"FROM package_catalogue WHERE id IN ({q})", ids) as cur:
            return {(r[0], r[1], r[2]): r[3] for r in await cur.fetchall()}
    finally:
        await db.close()


builder = Builder()


# --- API views -------------------------------------------------------------------

LAST_BUILD_LINES = 200


def _last_build(r: dict | None) -> dict | None:
    """The most recent finished build of a variant, from its version row (so it
    survives a restart): {ok, error, finished_at, log_tail: [<= 200 lines]}.
    The log lines are untrusted guest output."""
    if r is None:
        return None
    log, err = r.get("build_log") or "", None
    ok = r["status"] == "built"
    if not ok:
        # build() appends "\nERROR: <err>" to a failed build's log. Only a
        # failed row is parsed, so a guest line cannot forge an error on a good one.
        if "\nERROR: " in log:
            log, err = log.rsplit("\nERROR: ", 1)
        err = err or "build failed"
    lines = log.splitlines()
    return {"version": r["version"], "ok": ok, "error": err,
            "finished_at": r["built_at"], "log_tail": lines[-LAST_BUILD_LINES:]}


async def list_images(db) -> dict:
    from .. import packages
    info = await sync_variants(db)
    in_use: dict[tuple, list[str]] = {}
    for b in boxes.all_boxes():
        in_use.setdefault((b.image[0], b.image[1]), []).append(b.id)
    out = []
    for name, v in sorted(info.items()):
        async with db.execute("SELECT * FROM image_versions WHERE variant = ? "
                              "ORDER BY version DESC", (name,)) as cur:
            vers = [dict(r) for r in await cur.fetchall()]
        used = await packages.variant_used_by(db, name)
        rows = []
        for r in vers:
            ids = in_use.get((name, str(r["version"])), []) + in_use.get((name, f"v{r['version']}"), [])
            if r["active"]:
                ids += in_use.get((name, None), [])
            rows.append({"version": r["version"], "base_version": r["base_version"],
                         "size_bytes": r["size_bytes"], "built_at": r["built_at"],
                         "status": r["status"], "active": bool(r["active"]),
                         "recipe_sha256": r["recipe_sha256"],
                         "in_use_by": sorted(set(ids))})
        active = next((r for r in vers if r["active"]), None)
        done = next((r for r in vers if r["status"] in ("built", "failed")), None)
        out.append({"name": name, "from": v["from"], "builtin": v["builtin"],
                    "recipe": render_recipe({**v["own"], "packages": v["effective"]}),
                    "recipe_sha256": v["sha"], "min_mem_mb": v["min_mem_mb"],
                    "layer_packages": [_pin(p) for p in v["layer"]],
                    "needs_build": bool(v["layer"]) and (active is None or
                                                         active["recipe_sha256"] != v["sha"]),
                    "used_by": used["all"], "versions": rows,
                    "last_build": _last_build(done)})
    return {"variants": out, "build": builder.state()}


# --- registration (import time) -------------------------------------------------

def _register() -> None:
    boxes.add_image_resolver(resolve_image)
    from . import gateway_server
    reg_pkg = getattr(gateway_server, "register_package_builder", None)
    if reg_pkg is not None:
        reg_pkg("builder", builder.package)
    reg_op = getattr(gateway_server, "register_op_handler", None)
    if reg_op is not None:
        reg_op("build_report", builder.on_report)
    reg_floor = getattr(boxes, "add_mem_floor", None)
    if reg_floor is not None:
        reg_floor(mem_floor)
    from . import procview
    procview.add_baseline_resolver(baseline_path_for)


def baseline_path_for(box) -> Path | None:
    """procview baseline resolver: <image>.baseline.json beside the qcow2 the
    box runs (procview converts this WP5 shape into its (exe, unit) entries)."""
    try:
        p = baseline_file(boxes.image_path(box))
    except Exception:  # noqa: BLE001 — no image: procview falls back
        return None
    return p if p.exists() else None


def mem_floor(variant: str) -> int | None:
    """boxes.add_mem_floor hook. A recipe's min_mem_mb is a FLOOR only where it
    exceeds the base variant's (desktop and what is built from it): main, dev
    and svc declare the ordinary default, which must not override a smaller
    box_mem_mb a profile chose on purpose (4 GB host)."""
    need = min_mem_mb(variant)
    base = min_mem_mb(BASE_VARIANT) or 0
    return need if need and need > base else None


_register()
