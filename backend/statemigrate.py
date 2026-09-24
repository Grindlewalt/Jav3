"""Move a pre-state-dir install (durable state inside the code checkout) into
JARVIS_STATE_DIR: `python -m backend.cli migrate-state [--to DIR]`.

Never automatic. `config.ensure_dirs` only WARNS when it finds the old layout, so
a deploy can't strand a running box; this command is the one place data moves,
and it does so the cautious way: refuse while Jarvis is running, copy (hard-link
where the filesystem allows, so multi-GB guest images move instantly), snapshot
the DB through SQLite's backup API, verify every file and every table's row
count, and only then remove the source. Files git tracks (the shipped skills,
the .gitkeep placeholders) stay in the checkout.
"""
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .config import STATE_DIRS, has_state, settings

_DB_FILES = {"jarvis.db", "jarvis.db-wal", "jarvis.db-shm"}


class MigrateError(RuntimeError):
    pass


def _unit_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "jarvis"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() in ("active", "activating", "reloading")


def service_busy(db_path: Path) -> str | None:
    """Why a live Jarvis appears to own `db_path`, or None when it is safe to
    move/replace. The unit check catches an idle server (which holds no DB
    connection between requests); the lock probe catches anything mid-write,
    and a WAL checkpoint that cannot complete means a reader is still attached."""
    if _unit_active():
        return ("jarvis.service is running — stop it first: "
                "systemctl --user stop jarvis")
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path, timeout=0)
    try:
        con.execute("BEGIN EXCLUSIVE")
        con.rollback()
        busy = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy:
            return f"{db_path} still has an active reader — is Jarvis running?"
    except sqlite3.OperationalError as e:
        return f"{db_path} is locked ({e}) — is Jarvis running?"
    finally:
        con.close()
    return None


def snapshot_db(src: Path, dst: Path) -> None:
    """A consistent copy of a live WAL database (a plain file copy can tear or
    miss the -wal and open corrupt). The Pi has no sqlite3 CLI, hence Python."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    con = sqlite3.connect(src)
    out = sqlite3.connect(dst)
    try:
        with out:
            con.backup(out)
    finally:
        out.close()
        con.close()


def table_counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(db)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                for n in names}
    finally:
        con.close()


def integrity_ok(db: Path) -> bool:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()


def _link_or_copy(src: str, dst: str) -> str:
    try:
        os.link(src, dst)
        return dst
    except OSError:
        return shutil.copy2(src, dst)


def _tracked(base: Path) -> set[str] | None:
    """Paths under the state dirs that the checkout's git tracks (these ship
    with the code and must not be deleted from it). None = cannot tell."""
    try:
        r = subprocess.run(["git", "-C", str(base), "ls-files", "-z", "--",
                            *STATE_DIRS], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {p for p in r.stdout.decode().split("\0") if p}


def _files(root: Path) -> dict[str, int]:
    """relpath -> size (-1 for a symlink) of every file under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = -1 if p.is_symlink() else p.stat().st_size
    return out


def migrate_state(to: Path | None = None) -> list[str]:
    """Move the checkout's state into `to` (default: the configured state dir).
    Returns human-readable progress lines; raises MigrateError on refusal."""
    src = settings.base_dir.resolve()
    dst = Path(to or settings.state_dir).expanduser().resolve()
    if dst == src:
        raise MigrateError("the target is the code checkout itself — nothing to do")
    if not has_state(src):
        raise MigrateError(f"no legacy state under {src} — nothing to migrate")
    if has_state(dst):
        raise MigrateError(f"{dst} already holds Jarvis state — refusing to merge "
                           "into it. Move it aside or pick another --to.")
    src_db = src / "data" / "jarvis.db"
    why = service_busy(src_db)
    if why:
        raise MigrateError(why)

    log = []
    dst.mkdir(parents=True, exist_ok=True)
    for name in STATE_DIRS:
        s = src / name
        if not s.is_dir():
            continue
        ignore = shutil.ignore_patterns(*_DB_FILES) if name == "data" else None
        shutil.copytree(s, dst / name, symlinks=True, dirs_exist_ok=True,
                        ignore=ignore, copy_function=_link_or_copy)
        log.append(f"copied {name}/")
    if src_db.exists():
        dst_db = dst / "data" / "jarvis.db"
        snapshot_db(src_db, dst_db)
        # projects.path is informational (every reader joins projects_dir/slug),
        # but keep it truthful.
        con = sqlite3.connect(dst_db)
        with con:
            con.execute("UPDATE projects SET path = replace(path, ?, ?)",
                        (str(src / "projects"), str(dst / "projects")))
        con.close()
        log.append("snapshotted data/jarvis.db")

    # verify before anything is removed
    for name in STATE_DIRS:
        s = src / name
        if not s.is_dir():
            continue
        have = _files(dst / name)
        for rel, size in _files(s).items():
            if name == "data" and rel in _DB_FILES:
                continue
            if rel not in have or (size >= 0 and have[rel] != size):
                raise MigrateError(f"verify failed: {name}/{rel} did not copy "
                                   f"intact — source left untouched in {src}")
    if src_db.exists():
        dst_db = dst / "data" / "jarvis.db"
        if not integrity_ok(dst_db):
            raise MigrateError("verify failed: the copied DB fails integrity_check "
                               f"— source left untouched in {src}")
        if table_counts(src_db) != table_counts(dst_db):
            raise MigrateError("verify failed: table row counts differ — source "
                               f"left untouched in {src}")
    log.append("verified every file and every table's row count")

    tracked = _tracked(src)
    for name in STATE_DIRS:
        s = src / name
        if not s.is_dir():
            continue
        if name == "data":
            shutil.rmtree(s)          # wholly gitignored runtime state
            continue
        if tracked is None and name == "skills":
            log.append("left skills/ in the checkout (git unavailable to tell "
                       "shipped skills from yours)")
            continue
        for entry in s.iterdir():
            rel = f"{name}/{entry.name}"
            if entry.name == ".gitkeep" or (tracked is not None and any(
                    t == rel or t.startswith(rel + "/") for t in tracked)):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    log.append(f"removed the old copies from {src}")
    if dst != settings.state_dir.expanduser().resolve():
        log.append(f"set JARVIS_STATE_DIR={dst} in ~/.config/jarvis/env before "
                   "starting Jarvis")
    return log
