"""Back up / restore Jarvis's durable state with rclone (https://rclone.org, MIT).

What goes up, under the configured remote path:
  memory/ projects/ agents/ skills/   `rclone sync` of each state dir
  data/jarvis.db                      a consistent SQLite snapshot (backup API),
                                      never the live WAL file
  secrets/                            ONLY with backup_include_secrets, and ONLY
                                      through an rclone crypt layer: the env file,
                                      secrets.json and the JWT secret

Plaintext secrets are never uploaded. With the toggle on, either
`backup_crypt_remote` names a crypt remote the user defined in their own rclone
config, or one is built on the fly over <remote>/secrets from
`backup_crypt_password` (+ optional password2) — handed to rclone as
RCLONE_CONFIG_* environment variables, so the password is never on a command
line. Neither configured = the backup refuses.

The rclone config itself stays the user's, at rclone's default path. The
settings in backend/config.py are defaults; PUT /api/backup/config overlays them
in ~/.config/jarvis/backup.json (0600, next to the env file and outside the
state dir, so it is not itself backed up in the clear).
"""
import asyncio
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_same_origin, require_user
from .config import has_state, settings
from .statemigrate import integrity_ok, service_busy, snapshot_db

# The file-tree state dirs, by their name on the remote.
DIRS = ("memory", "projects", "agents", "skills")
EXCLUDES = ("__pycache__/**", "*.pyc", ".venv/**", "node_modules/**",
            ".ephemeral-notes/**")
CRYPT_NAME = "JAV3CRYPT"          # the on-the-fly crypt remote's config name
# One line, surfaced by /status for the GUI's empty state and by the refusals.
# A pointer rather than a distro command: the install differs per system.
INSTALL_HINT = "see https://rclone.org/install/"
_CONFIG_KEYS = ("remote", "rclone", "include_secrets", "crypt_remote",
                "crypt_password", "crypt_password2")


class BackupError(RuntimeError):
    pass


# ------------------------------------------------------------------ config ---

def _config_path() -> Path:
    return settings.secrets_path.parent / "backup.json"


def _status_path() -> Path:
    return settings.data_dir / "backup-status.json"


def load_config() -> dict:
    cfg = {k: getattr(settings, f"backup_{k}") for k in _CONFIG_KEYS}
    try:
        saved = json.loads(_config_path().read_text())
    except (OSError, json.JSONDecodeError):
        saved = {}
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if k in _CONFIG_KEYS})
    return cfg


def save_config(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({k: cfg[k] for k in _CONFIG_KEYS}, f, indent=2)
    p.chmod(0o600)


def public_config(cfg: dict) -> dict:
    """What the API may show: never a password, only whether one is set."""
    return {"remote": cfg["remote"], "rclone": cfg["rclone"],
            "include_secrets": cfg["include_secrets"],
            "crypt_remote": cfg["crypt_remote"],
            "crypt_password_set": bool(cfg["crypt_password"]),
            "crypt_password2_set": bool(cfg["crypt_password2"]),
            "crypt_configured": crypt_configured(cfg)}


def crypt_configured(cfg: dict) -> bool:
    return bool(cfg["crypt_remote"] or cfg["crypt_password"])


def valid_remote(value: str) -> bool:
    """An rclone remote path goes on rclone's argv: a leading '-' would be read
    as a flag, and control characters have no business in it."""
    return bool(value) and not value.startswith("-") and value.isprintable()


def _join(remote: str, sub: str) -> str:
    remote = remote.rstrip("/")
    return f"{remote}{sub}" if remote.endswith(":") else f"{remote}/{sub}"


def rclone_path(cfg: dict | None = None) -> str | None:
    return shutil.which((cfg or load_config())["rclone"] or "rclone")


# ------------------------------------------------------------------ rclone ---

def _rclone(rclone: str, args: list[str], env: dict | None = None,
            ok_codes: tuple[int, ...] = (0,)) -> int:
    r = subprocess.run([rclone, *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if r.returncode not in ok_codes:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise BackupError(f"rclone {args[0]} failed (exit {r.returncode}): "
                          + " | ".join(tail))
    return r.returncode


def _obscure(rclone: str, secret: str) -> str:
    """rclone wants crypt passwords obscured; `obscure -` reads stdin so the
    plaintext never lands on a command line."""
    r = subprocess.run([rclone, "obscure", "-"], input=secret,
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise BackupError("rclone obscure failed — cannot build the crypt remote")
    return r.stdout.strip()


def _crypt_target(cfg: dict, rclone: str) -> tuple[str, dict]:
    """(rclone path, extra env) of the encrypted secrets destination. Raises
    rather than ever returning a plaintext location."""
    if cfg["crypt_remote"]:
        return cfg["crypt_remote"], {}
    if not cfg["crypt_password"]:
        raise BackupError(
            "backup_include_secrets is on but no rclone crypt layer is configured "
            "— set a crypt password (or name your own crypt remote). Refusing to "
            "upload secrets in plaintext.")
    env = {f"RCLONE_CONFIG_{CRYPT_NAME}_TYPE": "crypt",
           f"RCLONE_CONFIG_{CRYPT_NAME}_REMOTE": _join(cfg["remote"], "secrets"),
           f"RCLONE_CONFIG_{CRYPT_NAME}_PASSWORD": _obscure(rclone, cfg["crypt_password"])}
    if cfg["crypt_password2"]:
        env[f"RCLONE_CONFIG_{CRYPT_NAME}_PASSWORD2"] = _obscure(
            rclone, cfg["crypt_password2"])
    return f"{CRYPT_NAME.lower()}:", env


def _secret_files() -> dict[str, Path]:
    """Backup name -> live path of everything secret."""
    return {"env": Path(os.path.expanduser("~/.config/jarvis/env")),
            "secrets.json": settings.secrets_path,
            "jwt_secret": settings.data_dir / "jwt_secret"}


def _preflight(cfg: dict) -> str:
    if not valid_remote(cfg["remote"]):
        raise BackupError("no backup remote configured (e.g. myremote:jav3-backup)")
    rclone = rclone_path(cfg)
    if not rclone:
        raise BackupError(f"rclone is not installed ('{cfg['rclone']}' not on PATH) "
                          f"— {INSTALL_HINT}")
    if cfg["include_secrets"] and not crypt_configured(cfg):
        _crypt_target(cfg, rclone)          # raises the plaintext refusal
    return rclone


# ------------------------------------------------------------ run / status ---

@contextlib.contextmanager
def _run_lock():
    """One backup/restore at a time across processes (the timer's CLI run and
    an API run share the state dir)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    f = open(settings.data_dir / ".backup.lock", "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("a backup or restore is already running") from None
        yield
    finally:
        f.close()


def is_running() -> bool:
    try:
        with _run_lock():
            return False
    except BackupError:
        return True


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".venv", "node_modules",
                                    ".ephemeral-notes")]
        for f in filenames:
            if not f.endswith(".pyc"):
                with contextlib.suppress(OSError):
                    total += (Path(dirpath) / f).lstat().st_size
    return total


def _write_status(status: dict) -> None:
    p = _status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_backup() -> dict:
    """Sync the durable state to the configured remote. Returns the status
    record written to data/backup-status.json; raises BackupError on failure
    (the failure is recorded in the status too)."""
    cfg = load_config()
    rclone = _preflight(cfg)
    with _run_lock():
        t0, status = time.monotonic(), {"started_at": _now(), "remote": cfg["remote"],
                                        "include_secrets": cfg["include_secrets"]}
        try:
            n = 0
            for name in DIRS:
                src = getattr(settings, f"{name}_dir")
                if not src.is_dir():
                    continue
                args = ["sync", str(src), _join(cfg["remote"], name)]
                for pat in EXCLUDES:
                    args += ["--exclude", pat]
                _rclone(rclone, args)
                n += _tree_bytes(src)
            with tempfile.TemporaryDirectory(prefix=".backup-",
                                             dir=settings.data_dir) as tmp:
                tmp = Path(tmp)
                if settings.db_path.exists():
                    snap = tmp / "jarvis.db"
                    snapshot_db(settings.db_path, snap)
                    n += snap.stat().st_size
                    # --checksum: an unchanged DB snapshots to identical bytes, so
                    # it isn't re-uploaded just for its fresh mtime.
                    _rclone(rclone, ["copyto", "--checksum", str(snap),
                                     _join(cfg["remote"], "data/jarvis.db")])
                if cfg["include_secrets"]:
                    target, env = _crypt_target(cfg, rclone)
                    sec = tmp / "secrets"
                    sec.mkdir(mode=0o700)
                    for bname, live in _secret_files().items():
                        if live.is_file():
                            shutil.copy2(live, sec / bname)
                            n += live.stat().st_size
                    _rclone(rclone, ["sync", str(sec), target], env=env)
            status.update(ok=True, error=None, bytes=n)
        except (BackupError, OSError) as e:
            status.update(ok=False, error=str(e))
            raise BackupError(str(e)) from None
        finally:
            status.update(finished_at=_now(),
                          seconds=round(time.monotonic() - t0, 1))
            _write_status(status)
    return status


def next_scheduled() -> str | None:
    """The timer's next run as UTC ISO-8601, like the status record's times.
    `list-timers -o json` gives epoch microseconds. `show
    NextElapseUSecRealtime` was empty for this monotonic (OnUnitActiveSec)
    timer, and where set it is a locale-shaped "Thu … BST" nothing parses."""
    try:
        r = subprocess.run(["systemctl", "--user", "list-timers",
                            "jarvis-backup.timer", "--all", "-o", "json"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        usec = json.loads(r.stdout)[0]["next"]
        return datetime.fromtimestamp(int(usec) / 1e6, timezone.utc) \
            .isoformat(timespec="seconds") if usec else None
    except (ValueError, TypeError, LookupError, OverflowError, OSError):
        return None


def status() -> dict:
    cfg = load_config()
    rclone = rclone_path(cfg)
    try:
        last = json.loads(_status_path().read_text())
    except (OSError, json.JSONDecodeError):
        last = None
    return {"rclone": {"available": bool(rclone), "path": rclone,
                       "install_hint": None if rclone else INSTALL_HINT},
            "configured": valid_remote(cfg["remote"]),
            "remote": cfg["remote"],
            "include_secrets": cfg["include_secrets"],
            "running": is_running(),
            "last": last,
            "next_scheduled": next_scheduled()}


# ----------------------------------------------------------------- restore ---

def restore(from_remote: str | None = None, to_dir: Path | None = None,
            include_secrets: bool | None = None, force: bool = False) -> list[str]:
    """Pull a backup into a state-dir layout at `to_dir` (default: the
    configured state dir). Refuses to land on existing state unless `force`,
    and refuses while Jarvis runs on it. Restored secrets are installed only
    where no file exists yet; otherwise they wait in data/.restored-secrets/."""
    cfg = load_config()
    remote = from_remote or cfg["remote"]
    if not valid_remote(remote):
        raise BackupError("no remote to restore from")
    rclone = rclone_path(cfg)
    if not rclone:
        raise BackupError(f"rclone is not installed — {INSTALL_HINT}")
    to = Path(to_dir or settings.state_dir).expanduser().resolve()
    if has_state(to) and not force:
        raise BackupError(f"{to} already holds Jarvis state — refusing to "
                          "overwrite it (pass --force to restore over it)")
    why = service_busy(to / "data" / "jarvis.db")
    if why:
        raise BackupError(why)
    secrets_on = cfg["include_secrets"] if include_secrets is None else include_secrets
    log = []
    with _run_lock():
        for name in DIRS:
            # exit 3 = directory not found: that dir was empty/absent at backup time
            if _rclone(rclone, ["copy", _join(remote, name), str(to / name)],
                       ok_codes=(0, 3)) == 0:
                log.append(f"restored {name}/")
        data = to / "data"
        data.mkdir(parents=True, exist_ok=True)
        incoming = data / "jarvis.db.restore"
        _rclone(rclone, ["copyto", _join(remote, "data/jarvis.db"), str(incoming)])
        if not integrity_ok(incoming):
            incoming.unlink(missing_ok=True)
            raise BackupError("the backed-up DB fails integrity_check — not installed")
        for suffix in ("-wal", "-shm"):
            (data / f"jarvis.db{suffix}").unlink(missing_ok=True)
        incoming.replace(data / "jarvis.db")
        log.append("restored data/jarvis.db (integrity ok)")
        if secrets_on:
            target, env = _crypt_target({**cfg, "remote": remote}, rclone)
            staged = data / ".restored-secrets"
            staged.mkdir(mode=0o700, exist_ok=True)
            _rclone(rclone, ["copy", target, str(staged)], env=env)
            live = {**_secret_files(), "jwt_secret": data / "jwt_secret"}
            for bname, dest in live.items():
                got = staged / bname
                if not got.is_file():
                    continue
                got.chmod(0o600)
                if dest.exists():
                    log.append(f"kept existing {dest}; the backed-up copy is in {got}")
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(got, dest)
                    log.append(f"installed {dest}")
    return log


# --------------------------------------------------------------------- API ---

router = APIRouter(prefix="/api/backup", tags=["backup"],
                   dependencies=[Depends(require_user)])
_task: asyncio.Task | None = None


class BackupConfig(BaseModel):
    # every field optional: an omitted one keeps its value. For the passwords,
    # "" clears and None keeps — they are write-only (GET never returns them).
    remote: str | None = None
    rclone: str | None = None
    include_secrets: bool | None = None
    crypt_remote: str | None = None
    crypt_password: str | None = None
    crypt_password2: str | None = None


@router.get("/status")
async def get_status():
    return await asyncio.to_thread(status)


@router.get("/config")
async def get_config():
    return public_config(load_config())


@router.put("/config", dependencies=[Depends(require_same_origin)])
async def put_config(body: BackupConfig):
    cfg = load_config()
    for k, v in body.model_dump(exclude_none=True).items():
        cfg[k] = v.strip() if isinstance(v, str) else v
    for k in ("remote", "crypt_remote"):
        if cfg[k] and not valid_remote(cfg[k]):
            raise HTTPException(status_code=400, detail=f"invalid {k}")
    if cfg["include_secrets"] and not crypt_configured(cfg):
        raise HTTPException(status_code=400, detail=(
            "including secrets needs an rclone crypt layer — set a crypt "
            "password or a crypt remote first"))
    save_config(cfg)
    return public_config(cfg)


@router.post("/run", dependencies=[Depends(require_same_origin)])
async def post_run():
    """Start a backup in the background; poll /status for the result."""
    global _task
    cfg = load_config()
    try:
        _preflight(cfg)
    except BackupError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if (_task and not _task.done()) or is_running():
        raise HTTPException(status_code=409, detail="a backup is already running")
    _task = asyncio.create_task(asyncio.to_thread(_run_quietly))
    return {"started": True}


def _run_quietly() -> None:
    with contextlib.suppress(BackupError):     # recorded in the status file
        run_backup()
