"""What the operator can DO about an event, beyond acknowledging it (SB2).

  revert_file   a write flag: put the file back to git HEAD, or delete it when
                the agent created it. Refuses when the file changed since the
                event, when there is nothing committed to go back to, or when
                nothing was written (a refused secret leak).
  uncut_host    an anomaly cut: lift the cut and the nftables drop; the host is
                not re-judged by the anomaly detectors for an hour.
  kill_process  an unexpected process: signal it, but only if it still is the
                process the last ps snapshot named (procview.kill_process).
  stop          the agent that raised the event (`agent`), or the whole run it
                belongs to (`run`, which must be confirmed).

Each one raises ActionRefused with a sentence for the operator when it will not
act: the routes turn that into a 409. A successful action files an audit line
(info, by the operator: recorded, nothing to do) and acknowledges the event it
dealt with. Nothing here trusts the event's text beyond naming a file, a host or
a pid, and each of those is checked against the real thing before it is touched.
"""
import asyncio
import hashlib
import os
from pathlib import Path

import aiosqlite
from fastapi import HTTPException

from . import egress, gitgate, security, secruns, writes
from .config import settings
from .fsutil import safe_join


class ActionRefused(ValueError):
    """The action will not run; the text says why, for the operator."""


def _detail(ev: dict) -> dict:
    d = ev.get("detail")
    return d if isinstance(d, dict) else {}


async def _audit(db, kind: str, summary: str, detail: dict, ev: dict) -> None:
    """A line for the History: you did this. Filed already acknowledged (by you)."""
    await security.raise_event(db, kind=kind, severity="info", summary=summary,
                               project=ev.get("project_slug"), detail=detail,
                               actor=security.OPERATOR,
                               conversation_id=secruns.event_conversation(ev))


# --- revert a file --------------------------------------------------------------------

async def _git_bytes(slug: str, *args: str) -> tuple[int, bytes]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(gitgate._project_dir(slug)), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ActionRefused("git did not answer in time")
    return proc.returncode, out


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _event_time(ev: dict) -> float | None:
    d = security._parse_when(ev.get("last_seen") or ev.get("created_at"))
    return d.timestamp() if d else None


async def revert_file(db: aiosqlite.Connection, ev: dict) -> dict:
    """See the module doc. Returns {"action": "restored" | "deleted", "path", "bytes"}."""
    if ev.get("kind") != "write_flag":
        raise ActionRefused("only a write flag names a file to revert")
    d = _detail(ev)
    rel, slug = d.get("path"), ev.get("project_slug")
    if not isinstance(rel, str) or not rel or not slug:
        raise ActionRefused("this alert does not name a file in a project")
    if d.get("refused"):
        raise ActionRefused("that write was refused, so nothing was written to revert")
    project = settings.projects_dir / slug
    try:
        dest = safe_join(project, rel)          # raises HTTPException(400) for an escape
        top = dest.relative_to(project.resolve()).parts[0]
    except (HTTPException, ValueError, IndexError):
        raise ActionRefused("that path is outside the project")
    if top in writes.PROTECTED or top.casefold() == ".context.json":
        raise ActionRefused(f"{top} is not something an agent's write can change")
    if not (project / ".git").exists():
        raise ActionRefused("this project has no git history to go back to")
    rc, _ = await _git_bytes(slug, "rev-parse", "--verify", "-q", "HEAD")
    has_head = rc == 0
    in_head = False
    if has_head:
        rc, _ = await _git_bytes(slug, "cat-file", "-e", f"HEAD:{rel}")
        in_head = rc == 0
    exists = dest.is_file()
    if d.get("deleted"):
        if exists:
            raise ActionRefused(f"{rel} is back: it changed since this alert, not touching it")
        if not in_head:
            raise ActionRefused(f"{rel} was never committed, so there is no copy to bring back")
        action = "restored"
    else:
        if not exists:
            raise ActionRefused(f"{rel} is gone: it changed since this alert, not touching it")
        sha = d.get("sha")
        if sha:
            if _sha(dest) != sha:
                raise ActionRefused(f"{rel} has changed since this alert: not touching it. "
                                    "Look at the diff, or revert it in git.")
        else:                                    # an alert from before the file's fingerprint
            at = _event_time(ev)
            if at is not None and dest.stat().st_mtime > at + 5:
                raise ActionRefused(f"{rel} has changed since this alert: not touching it. "
                                    "Look at the diff, or revert it in git.")
        if in_head:
            action = "restored"
        elif d.get("new_file"):
            action = "deleted"
        else:
            raise ActionRefused(f"{rel} was never committed and the agent did not create it, "
                                "so there is no earlier version to restore")
    if action == "restored":
        rc, data = await _git_bytes(slug, "show", f"HEAD:{rel}")
        if rc != 0:
            raise ActionRefused("git could not read the committed copy")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o644)
        size = len(data)
    else:
        dest.unlink()
        size = 0
        project_r = project.resolve()
        parent = dest.parent
        while parent != project_r and parent.is_relative_to(project_r):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    await _audit(db, "write_reverted",
                 f"You {'restored' if action == 'restored' else 'deleted'} {rel} "
                 f"({'to the committed copy' if action == 'restored' else 'it was new'})",
                 {"path": rel, "action": action, "from_event": ev["id"]}, ev)
    await security.acknowledge(db, ev["id"])
    return {"action": action, "path": rel, "bytes": size}


# --- un-cut a host --------------------------------------------------------------------

async def uncut_host(db: aiosqlite.Connection, ev: dict) -> dict:
    """Lift an auto-cut: the proxy's block, the nftables drop (the IPs the cut
    recorded, plus what the name resolves to now), and the anomaly detectors skip
    the host for egress.UNCUT_GRACE_S. Returns {"host", "was_cut", "ips"}."""
    if ev.get("kind") not in ("egress_anomaly", "host_cut"):
        raise ActionRefused("only a cut host can be un-cut")
    d = _detail(ev)
    host = d.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ActionRefused("this alert does not name a host")
    slug = ev.get("project_slug")
    was = egress.uncut(slug, host)
    from .vm import egress_proxy
    ips = await egress_proxy.nft_undrop(host, d.get("dropped_ips"))
    await _audit(db, "host_uncut", f"You un-cut {host}: the anomaly checks skip it for "
                 f"{egress.UNCUT_GRACE_S // 60} minutes",
                 {"host": host, "ips": ips, "from_event": ev["id"]}, ev)
    await security.acknowledge(db, ev["id"])
    return {"host": host, "was_cut": was, "ips": ips,
            "grace_minutes": egress.UNCUT_GRACE_S // 60}


# --- kill a process -------------------------------------------------------------------

async def kill_process(db: aiosqlite.Connection, ev: dict, sig: str = "TERM") -> dict:
    """Signal the process an unexpected_process alert named, if it still is that
    process (see procview.kill_process). Returns {"pid", "sig", "box_id"}."""
    if ev.get("kind") not in ("unexpected_process", "proc_report_mismatch"):
        raise ActionRefused("only an alert about a process can kill one")
    d = _detail(ev)
    pid = d.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ActionRefused("this alert does not name a process")
    box, boot = secruns.event_box(ev)
    from .vm import procview
    try:
        out = await procview.kill_process(
            box or "", pid, str(d.get("exe") or ""), cmd=d.get("cmd"),
            start_ticks=d.get("start_ticks") if isinstance(d.get("start_ticks"), int) else None,
            boot_id=boot, sig=sig)
    except procview.KillRefused as e:
        raise ActionRefused(str(e))
    await _audit(db, "process_killed",
                 f"You stopped {d.get('exe') or 'a process'} (pid {pid}) in box {box}",
                 {"box_id": box, "pid": pid, "exe": d.get("exe"), "sig": sig,
                  "from_event": ev["id"]}, ev)
    await security.acknowledge(db, ev["id"])
    return out


# --- stop the agent, or the whole run -------------------------------------------------

async def _tree(db, root: int) -> set[int]:
    async with db.execute(
            "WITH RECURSIVE tree(id) AS (SELECT id FROM conversations WHERE id = ? "
            "UNION SELECT c.id FROM conversations c JOIN tree t "
            "ON c.parent_conversation_id = t.id) SELECT id FROM tree", (root,)) as cur:
        return {r["id"] for r in await cur.fetchall()}


def _live() -> set[int]:
    try:
        from . import chat
        return set(chat._running_loops())
    except Exception:                               # noqa: BLE001
        return set()


async def stop(db: aiosqlite.Connection, ev: dict, scope: str, *, confirm: bool = False,
               dry_run: bool = False) -> dict:
    """Stop the agent that raised this event (`agent`: that conversation only) or
    the whole run (`run`: every live conversation under the run's root, and the
    plan runner when it is driving them). `run` only acts with confirm=True; a
    call without it, or with dry_run, says what it would stop and stops nothing.
    Returns {"stopped", "dry_run", "needs_confirm", "agents": [ids], "plans": [slugs],
    "message"}."""
    if scope not in ("agent", "run"):
        raise ActionRefused("scope must be agent or run")
    cid = secruns.event_conversation(ev)
    if cid is None:
        raise ActionRefused("this alert is not tied to an agent run, so there is nothing to stop")
    root = _int(ev.get("run_root")) or await security.run_root_of(db, cid) or cid
    tree = {cid} if scope == "agent" else await _tree(db, root)
    live = _live()
    ids = sorted(tree & live)
    plans: list[str] = []
    from . import plan as plan_mod
    if scope == "run":
        for c in ids:
            item = plan_mod.live_item(c)
            run = plan_mod._runs.get(item["project"]) if item else None
            if item and run is not None and not run.done() and item["project"] not in plans:
                plans.append(item["project"])
    out = {"scope": scope, "agents": ids, "plans": plans, "dry_run": bool(dry_run),
           "needs_confirm": False, "stopped": False, "root": root}
    if not ids and not plans:
        out["message"] = ("That agent is not running." if scope == "agent"
                          else "Nothing in this run is running.")
        return out
    if dry_run or (scope == "run" and not confirm):
        out["needs_confirm"] = scope == "run" and not dry_run
        out["message"] = (f"{len(ids)} agent{'s' * (len(ids) != 1)} running"
                          + (f" and the plan of {', '.join(plans)}" if plans else "")
                          + f" would stop (run {root}).")
        return out
    from . import agents_run, chat
    from .vm import broker as vm_broker
    for c in ids:
        chat._stop(c)
        t = agents_run._active_runs.get(c)
        if t is not None and not t.done():
            t.cancel()
    for s in plans:
        plan_mod.stop_run(s)
    vm_broker.cancel_conversations(set(ids))
    from . import operator_ask
    operator_ask.cancel_tree(set(ids))
    out["stopped"] = True
    out["message"] = (f"Stopped {len(ids)} agent{'s' * (len(ids) != 1)}"
                      + (f" and the plan of {', '.join(plans)}" if plans else "") + ".")
    await _audit(db, "run_stopped", f"You stopped {'the whole run' if scope == 'run' else 'an agent'}"
                 f" (run {root}): {out['message']}",
                 {"scope": scope, "agents": ids, "plans": plans, "root": root,
                  "from_event": ev["id"]}, ev)
    return out


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None

