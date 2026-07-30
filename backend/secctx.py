"""Security-event context boards — the evidence behind an alert.

A `security_events` row is a one-line summary plus a JSON blob: enough to
*alert*, useless to *judge*. When a card fires the operator's real question is
"what happened, to what, in what shape, and is it actually bad?" — and answering
it meant SSHing to the Pi. This module assembles the answer server-side: plain
English framing plus the live evidence around the event.

  write_flag        the flagged lines in place, the diff against git HEAD, the
                    directory the file lives in (with siblings from the same
                    write burst marked), the project's other uncommitted files,
                    and which conversation made the write
  egress_anomaly    the actual requests to that host, where the project's bytes
                    go (the baseline the spike was measured against), the
                    request cadence, current policy and cut state
  login_failed      whether the tried username is a real account, the peer, and
                    the history of bursts
  computeruse_auth  the rejected peer against the list of computers legitimately
                    connected right now
  anything else     the detail flattened into labelled facts, never a raw dump

Everything is read fresh at request time, so a board is honest about drift: it
says so when the file changed after the flag fired. Every string it returns is
UNTRUSTED (agent-written code, scanned hostnames, request paths) and the client
renders it as text nodes only.

Boards are *typed sections* — facts | code | diff | files | table | note — so
the frontend is one switch rather than a bespoke layout per kind, and a new
event kind gets a useful board without touching the GUI.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from . import diffgate, egress, gitgate
from .config import settings
from .fsutil import safe_join

MAX_CODE_LINES = 180     # total snippet lines across all blocks
CODE_CONTEXT = 3         # unflagged lines shown either side of a marked line
MAX_DIFF_LINES = 240
MAX_DIR_ENTRIES = 300
MAX_LINE_CHARS = 400     # one very long minified line must not be the payload
BURST_SECONDS = 120      # a sibling touched this close shared the write turn


# --- section constructors ----------------------------------------------------

def _facts(title: str, rows: list, note: str | None = None) -> dict:
    """rows: [label, value, hint?] — hint is a dim aside under the value."""
    return {"type": "facts", "title": title, "note": note,
            "rows": [{"label": r[0], "value": _s(r[1]),
                      "hint": (r[2] if len(r) > 2 else None)}
                     for r in rows if r[1] is not None and r[1] != ""]}


def _table(title: str, cols: list[str], rows: list[list], *,
           note: str | None = None, empty: str | None = None) -> dict:
    return {"type": "table", "title": title, "cols": cols, "note": note,
            "empty": empty, "rows": [[_s(c) for c in r] for r in rows]}


def _note(title: str, text: str) -> dict:
    return {"type": "note", "title": title, "text": text}


def _s(v) -> str | None:
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(v, default=str)


def _bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _ts(sql: str | None) -> float | None:
    """A SQLite datetime('now') string is UTC-naive; make it a real timestamp."""
    if not sql:
        return None
    try:
        return datetime.fromisoformat(str(sql).replace(" ", "T")).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ago(epoch: float | None, now: float | None = None) -> str:
    """Rough human distance — 'is this happening right now' is the question."""
    if not epoch:
        return ""
    now = now or datetime.now(timezone.utc).timestamp()
    d = now - epoch
    if d < 0:
        return "in the future"
    for lim, div, unit in ((90, 1, "second"), (5400, 60, "minute"),
                           (172800, 3600, "hour"), (None, 86400, "day")):
        if lim is None or d < lim:
            n = round(d / div)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return ""


# --- plain-English framing ---------------------------------------------------
# (title, what happened, why it is worth a look, what to check)

_BRIEF = {
    "new_import": {
        "title": "A new dependency appeared in a file",
        "what": "This write added an import that was not in the file before.",
        "why": "A new import is the cheapest supply-chain move there is: a "
               "typo-squatted package name, or a legitimate module pulled in "
               "to reach the network, the filesystem or a subprocess.",
        "checks": ["Is the module real, spelled the way you'd expect, and already "
                   "used elsewhere in this project?",
                   "Is it declared in requirements.txt / package.json, or a surprise?",
                   "Does the code around it actually need it, or does it look bolted on?"],
    },
    "network_call": {
        "title": "Code that talks to the network was added",
        "what": "This write added an outbound-call primitive (an HTTP client, a "
                "socket, a curl/wget shell-out).",
        "why": "Exfiltration, a beacon and a dependency download all look like "
               "this line. Inside the guest the egress proxy still gates where "
               "it can reach — this flag is about whether the code should be "
               "reaching out at all.",
        "checks": ["Is the destination a constant you recognise, or built at runtime?",
                   "Does anything secret sit in the request body or headers?",
                   "Is this code on a path that runs unattended (a schedule, a tool)?"],
    },
    "high_entropy": {
        "title": "An opaque blob was embedded in the code",
        "what": "This write added a long base64/hex run — random-looking enough "
                "that it is not prose and not source.",
        "why": "That is the shape of an embedded payload, a packed script, or a "
               "hard-coded credential that the secret scanner does not know about.",
        "checks": ["Is it obviously data the code needs (an image, a test fixture, a hash)?",
                   "Is anything decoding it and then executing or eval-ing it?",
                   "If it looks like a key, rotate it and move it into the secret store."],
    },
    "logging_removed": {
        "title": "Logging calls were removed",
        "what": "The file came out of this write with fewer logging calls than "
                "it went in with.",
        "why": "Turning the lights off is a step in a compromise, and it is also "
               "what a tidy-up refactor looks like. The diff tells you which.",
        "checks": ["Was the whole function that logged deleted, or only the log line?",
                   "Did anything else change in the same write?",
                   "Is the removed line on an error path?"],
    },
    "assertion_removed": {
        "title": "Assertions were removed",
        "what": "The file came out of this write with fewer assertions than it "
                "went in with.",
        "why": "Deleting a failing check is the fastest way to make a suite go "
               "green without fixing anything.",
        "checks": ["Was the assertion replaced by a better one, or just dropped?",
                   "Does the test still fail if you put it back?",
                   "Was the behaviour it guarded changed in the same write?"],
    },
    "secret_leak": {
        "title": "A real secret value was about to be written to a file",
        "what": "The content contained the literal value of one of your stored "
                "secrets. THE WRITE WAS REFUSED — nothing landed on disk.",
        "why": "This is the one hard block left in the write path. The "
               "{{secret:NAME}} indirection exists so a key never sits in a "
               "file the agent can read back or commit.",
        "checks": ["Did the value reach the agent's context, or was it constructed? "
                   "If it was in context, treat the key as exposed and rotate it.",
                   "Should this project hold a grant for that secret at all?",
                   "Check the egress feed for requests carrying it around the same time."],
    },
    "_write_unknown": {
        "title": "A write tripped a diff gate",
        "what": "The deterministic write-time scan flagged this file.",
        "why": "The write landed — the gate is advisory since the staging "
               "quarantine was removed. Git is the undo surface.",
        "checks": ["Read the diff below and decide whether it is what you asked for."],
    },
    "egress_high_entropy": {
        "title": "The guest reached a random-looking hostname",
        "what": "A hostname with high character entropy was contacted, then cut.",
        "why": "Domain-generation algorithms and DNS tunnels produce names like "
               "this; hand-registered domains almost never do.",
        "checks": ["Is it a real CDN/cloud name (those can look random)?",
                   "Which project and which run reached it?",
                   "Did anything leave — check bytes out below."],
    },
    "egress_volume_spike": {
        "title": "One host received far more data than the project's norm",
        "what": "Bytes out to this host went well past the baseline built from "
                "the project's other hosts, so the host was cut.",
        "why": "Bulk outbound to a single destination is what exfiltration looks "
               "like. It is also what a legitimate upload looks like.",
        "checks": ["Was a big upload expected in this project?",
                   "Compare against the per-host table below — is the baseline meaningful?",
                   "Look at the request paths: one big POST, or thousands of small ones?"],
    },
    "egress_beacon_cadence": {
        "title": "Traffic to one host is suspiciously regular",
        "what": "Requests to this host arrived on a near-perfect interval, so it "
                "was cut.",
        "why": "Human and app traffic is bursty; command-and-control check-ins "
               "are metronomic.",
        "checks": ["Is a poller or a heartbeat in this project supposed to run?",
                   "Is the interval one a human would pick (30s, 60s, 300s)?",
                   "Do the request paths and sizes repeat exactly?"],
    },
    "login_failed": {
        "title": "Repeated failed logins",
        "what": "A burst of failed password attempts hit the login endpoint.",
        "why": "If the username is real, someone is guessing at a known account. "
               "If it is not, it is background internet noise finding your host.",
        "checks": ["Was this you, on a device with a stale saved password?",
                   "Is the tried username one that exists (see below)?",
                   "If the host is exposed, consider whether it needs to be."],
    },
    "computeruse_auth": {
        "title": "Rejected computer-use pairing attempts",
        "what": "Something presented a bad pairing token to the desktop-client "
                "WebSocket, repeatedly.",
        "why": "That socket is the seam between Jarvis and your actual machines, "
               "and it authenticates with a token rather than a session cookie.",
        "checks": ["Is one of your own clients running with an old token?",
                   "Does the peer below look like your LAN or like the internet?",
                   "If it is not yours, rotate the pairing token."],
    },
}


def _brief(key: str, fallback: str) -> dict:
    b = _BRIEF.get(key)
    if b:
        return b
    return {"title": fallback, "what": "", "why": "", "checks": []}


# --- shared lookups ----------------------------------------------------------

async def _project_name(db: aiosqlite.Connection, slug: str | None) -> str | None:
    if not slug:
        return None
    async with db.execute("SELECT name FROM projects WHERE slug = ?", (slug,)) as cur:
        r = await cur.fetchone()
    return r["name"] if r else None


async def _conv_label(db: aiosqlite.Connection, cid) -> str | None:
    """Name the run behind an event: '(subagent #41) tidy the parser · job 8f2c'."""
    if not cid:
        return None
    async with db.execute("SELECT id, kind, summary, job_id FROM conversations "
                          "WHERE id = ?", (int(cid),)) as cur:
        r = await cur.fetchone()
    if r is None:
        return f"conversation #{cid} (deleted)"
    bits = [f"{r['kind'] or 'chat'} #{r['id']}"]
    if r["summary"]:
        bits.append(str(r["summary"])[:70])
    if r["job_id"]:
        bits.append(f"job {str(r['job_id'])[:8]}")
    return " · ".join(bits)


async def _related(db: aiosqlite.Connection, ev: dict, *, path: str | None = None,
                   host: str | None = None) -> dict:
    """Sibling events of the same kind, and every path this kind has flagged.

    `same` is the subject's own history (this file, this host); `flagged_paths`
    lets the directory listing mark which neighbours carry their own alerts.
    Defensive: a board is still worth showing without its neighbours."""
    out: dict = {"same": [], "flagged_paths": set()}
    try:
        async with db.execute(
                "SELECT id, severity, summary, detail, acknowledged, created_at "
                "FROM security_events WHERE kind = ? AND id != ? "
                "ORDER BY id DESC LIMIT 200", (ev["kind"], ev["id"])) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    except Exception:                               # noqa: BLE001
        return out
    for r in rows:
        try:
            d = json.loads(r["detail"]) if r["detail"] else {}
        except ValueError:
            d = {}
        r["_d"] = d if isinstance(d, dict) else {}
        p = r["_d"].get("path")
        if p:
            out["flagged_paths"].add(str(p))
        if ((path and p == path) or (host and r["_d"].get("host") == host)
                or (not path and not host)):
            out["same"].append(r)
    out["same"] = out["same"][:12]
    return out


def _related_table(title: str, rows: list[dict], *, subject: str) -> dict | None:
    if not rows:
        return None
    body = [[_iso(_ts(r["created_at"])) or r["created_at"], r["severity"],
             (r.get("_d") or {}).get("trigger") or (r.get("_d") or {}).get("host") or "",
             r["summary"], "seen" if r["acknowledged"] else "waiting"]
            for r in rows]
    return _table(title, ["When", "Severity", "What", "Summary", "State"], body,
                  note=f"other alerts touching {subject}")


# --- write_flag --------------------------------------------------------------

def _project_dir(slug: str) -> Path:
    return safe_join(settings.projects_dir, slug)


def _code_section(title: str, text: str, marks: list[int], *,
                  path: str, note: str | None = None) -> dict | None:
    """Marked lines with context, grouped into contiguous blocks."""
    lines = text.splitlines()
    if not lines:
        return None
    marked = {n for n in marks if 1 <= n <= len(lines)}
    if not marked:
        # no located line: the head of the file is better than nothing
        marked = set()
        wanted = list(range(1, min(len(lines), 40) + 1))
    else:
        wanted = sorted({n for m in marked
                         for n in range(max(1, m - CODE_CONTEXT),
                                        min(len(lines), m + CODE_CONTEXT) + 1)})
    wanted = wanted[:MAX_CODE_LINES]
    blocks, cur = [], None
    for n in wanted:
        if cur is None or n != cur["lines"][-1]["n"] + 1:
            cur = {"lines": []}
            blocks.append(cur)
        cur["lines"].append({"n": n, "text": lines[n - 1][:MAX_LINE_CHARS],
                             "mark": n in marked})
    return {"type": "code", "title": title, "path": path, "note": note,
            "blocks": blocks[:8],
            "truncated": len(wanted) >= MAX_CODE_LINES}


def _parse_hunks(text: str) -> tuple[list[dict], bool]:
    hunks: list[dict] = []
    cur, total = None, 0
    for ln in text.splitlines():
        if ln.startswith("@@"):
            cur = {"header": ln[:200], "lines": []}
            hunks.append(cur)
            continue
        if cur is None or not ln:
            continue
        if total >= MAX_DIFF_LINES:
            return hunks, True
        k = ln[0]
        if k not in "+- ":
            continue                          # '\ No newline at end of file'
        cur["lines"].append({"k": k, "text": ln[1:][:MAX_LINE_CHARS]})
        total += 1
    return [h for h in hunks if h["lines"]], False


async def _diff_section(slug: str, rel: str) -> dict | None:
    """The write as git sees it. Projects are git repos with a baseline commit,
    so uncommitted-vs-HEAD is usually exactly what the agent just did."""
    rc, out, err = await gitgate.run_git(slug, "diff", "--no-color", "-U3",
                                         "HEAD", "--", rel)
    if rc != 0:
        return _note("No git baseline",
                     "git could not diff this file against HEAD "
                     f"({(err or out).strip()[:200]}) — the project may have no "
                     "commits yet, so there is nothing to compare against.")
    if not out.strip():
        _, status, _ = await gitgate.run_git(slug, "status", "--porcelain", "--", rel)
        if status.strip().startswith("??"):
            return _note("New file, never committed",
                         "git has no version of this file, so the whole file is "
                         "the change — the snippet above is the diff.")
        return _note("Identical to the committed version",
                     "The working file matches git HEAD: whatever this write did "
                     "has since been committed, reverted, or rewritten.")
    hunks, truncated = _parse_hunks(out)
    return {"type": "diff", "title": "Uncommitted change vs git HEAD", "path": rel,
            "hunks": hunks, "truncated": truncated,
            "note": "`git diff HEAD` in the project repo — this is the undo surface"}


def _files_section(project: Path, path: Path, rel: str, event_ts: float | None,
                   flagged: set[str]) -> dict:
    """The directory the flagged file lives in — every file, with the ones from
    the same write burst and the ones carrying their own alerts marked."""
    d = path.parent
    entries: list[dict] = []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda q: (q.is_file(), q.name.lower())):
            if p.name == ".git":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            r = str(p.relative_to(project))
            is_dir = p.is_dir()
            count = None
            if is_dir:
                try:
                    count = sum(1 for _ in p.iterdir())
                except OSError:
                    count = None
            entries.append({
                "name": p.name, "kind": "dir" if is_dir else "file", "rel": r,
                "size": None if is_dir else st.st_size, "count": count,
                "mtime": _iso(st.st_mtime), "ago": _ago(st.st_mtime),
                "subject": r == rel, "flagged": r in flagged and r != rel,
                "burst": bool(not is_dir and r != rel and event_ts
                              and abs(st.st_mtime - event_ts) <= BURST_SECONDS),
            })
    shown = entries[:MAX_DIR_ENTRIES]
    burst = sum(1 for e in shown if e["burst"])
    note = f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in this directory"
    if burst:
        note += (f" · {burst} touched within {BURST_SECONDS}s of the flag "
                 "(same write burst)")
    if len(shown) < len(entries):
        note += f" · showing the first {len(shown)}"
    return {"type": "files", "title": f"{str(d.relative_to(project)) or '.'}/",
            "dir": str(d.relative_to(project)) or ".", "entries": shown, "note": note}


_MANIFESTS = ("requirements.txt", "requirements-dev.txt", "pyproject.toml",
              "Pipfile", "package.json", "go.mod", "Cargo.toml")


def _modules_section(project: Path, mods: list[str]) -> dict | None:
    """Classify each newly imported module: stdlib, declared in a manifest, or
    undeclared third-party — which is the typo-squat / injected-dependency tell.
    Answering this is the difference between "a new import" and "a new import of
    something nothing in this project has ever asked for"."""
    if not mods:
        return None
    import sys
    manifests = {}
    for name in _MANIFESTS:
        p = project / name
        try:
            if p.is_file():
                manifests[name] = p.read_text(errors="replace").lower()
        except OSError:
            continue
    rows = []
    for m in mods:
        m = str(m)
        if m.startswith((".", "/", "~")):
            verdict, where = "local file", "relative import, not a package"
        elif m in sys.stdlib_module_names:
            verdict, where = "python stdlib", "ships with python — nothing was installed"
        else:
            base = m.split("/")[0].lstrip("@")
            found = [n for n, text in manifests.items() if base.lower() in text]
            if found:
                verdict, where = "declared", ", ".join(found)
            else:
                verdict, where = ("NOT DECLARED",
                                  f"absent from {', '.join(manifests) or 'any manifest'}"
                                  " — check the spelling and whether it is installed")
        rows.append([m, verdict, where])
    return _table("The modules", ["Module", "Status", "Where"], rows,
                  note="undeclared third-party names are how typo-squats arrive")


_STATUS_CODES = {"??": "untracked", " M": "modified", "M ": "modified (staged)",
                 "A ": "added", " D": "deleted", "D ": "deleted (staged)",
                 "R ": "renamed", "MM": "modified", "AM": "added", "!!": "ignored"}


async def _status_section(slug: str) -> dict | None:
    """The project's whole uncommitted surface — the blast radius, not just the
    one flagged file."""
    rc, out, _ = await gitgate.run_git(slug, "status", "--porcelain", "-uall")
    if rc != 0:
        return None
    rows = []
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        rows.append([_STATUS_CODES.get(ln[:2], ln[:2].strip() or "changed"), ln[3:]])
    if not rows:
        return None
    return _table("Uncommitted files in this project", ["State", "Path"], rows[:60],
                  note="everything the agent has changed since the last commit"
                       + (f" · showing 60 of {len(rows)}" if len(rows) > 60 else ""))


async def _write_board(db, ev, detail, add) -> dict:
    slug = ev.get("project_slug")
    rel = str(detail.get("path") or "")
    trigger = str(detail.get("trigger") or "unknown")
    refused = bool(detail.get("refused")) or trigger == "secret_leak"
    brief = _brief(trigger, f"Write flag: {trigger}")
    event_ts = _ts(ev.get("created_at"))

    path = text = stat = None
    if slug and rel:
        try:
            project = _project_dir(slug)
            path = safe_join(project, rel)
        except Exception:
            await add(lambda: _note(
                "Path refused",
                f"The recorded path ({rel}) does not resolve inside the project "
                "directory, so nothing was read from disk."))
            path = None
    if path is not None and path.is_file():
        try:
            stat = path.stat()
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            text = None

    changed_after = bool(stat and event_ts and stat.st_mtime > event_ts + 2)

    async def facts():
        name = await _project_name(db, slug)
        rows = [
            ["File", rel],
            ["Project", f"{name} ({slug})" if name else slug],
            ["Gate", trigger, "deterministic write-time scan (backend/diffgate.py)"],
            ["Outcome",
             "REFUSED — nothing was written" if refused
             else "landed on disk — the gate is advisory, this is an alert not a block"],
            ["Flagged", f"{_iso(event_ts) or ev.get('created_at')} ({_ago(event_ts)})"],
            ["Written by", await _conv_label(db, detail.get("conversation_id"))
             or "not recorded (pre-dates write attribution)"],
        ]
        if detail.get("bytes") is not None:
            rows.append(["Write size", f"{_bytes(detail['bytes'])}"
                         + (f" · {detail['line_count']} lines"
                            if detail.get("line_count") else "")
                         + (" · new file" if detail.get("new_file") else "")])
        if stat:
            rows.append(["File now", f"{_bytes(stat.st_size)} · modified "
                                     f"{_iso(stat.st_mtime)} ({_ago(stat.st_mtime)})"])
        elif not refused:
            rows.append(["File now", "GONE — the file no longer exists on disk"])
        if changed_after:
            rows.append(["⚠ Drift", "the file was modified after this flag fired — "
                                    "what you see below is the current content, "
                                    "not the bytes that were scanned"])
        return _facts("The write", rows)

    async def tripped():
        rows = []
        if trigger == "new_import":
            rows.append(["Modules added", ", ".join(map(str, detail.get("modules") or []))])
        elif trigger == "network_call":
            rows.append(["Matched", ", ".join(map(str, detail.get("matches") or [])),
                         "the outbound-call primitives found in the added lines"])
        elif trigger == "high_entropy":
            rows.append(["Blobs", detail.get("count")])
            rows.append(["First blob", detail.get("sample")])
        elif trigger in ("logging_removed", "assertion_removed"):
            what = "logging calls" if trigger == "logging_removed" else "assertions"
            rows.append([f"{what.capitalize()} before", detail.get("before")])
            rows.append([f"{what.capitalize()} after", detail.get("after")])
        elif trigger == "secret_leak":
            rows.append(["Secrets found", ", ".join(map(str, detail.get("secrets") or [])),
                         "matched by VALUE against your secret store — the names "
                         "are shown, never the values"])
        if detail.get("lines"):
            rows.append(["At line" + ("s" if len(detail["lines"]) != 1 else ""),
                         ", ".join(str(n) for n in detail["lines"]),
                         "line numbers in the file as it was written"])
        return _facts("What tripped it", rows) if rows else None

    async def code():
        if refused or text is None:
            return None
        if trigger in ("logging_removed", "assertion_removed"):
            return None          # a removal has no line to point at; the diff has it
        stored = [int(n) for n in (detail.get("lines") or []) if isinstance(n, int)]
        marks, note = stored, None
        if not stored or changed_after:
            marks = diffgate.locate(text, trigger, detail)
            note = ("re-found in the current file" if stored else
                    "located by rescanning the current file (this event pre-dates "
                    "line recording)")
        if not marks:
            note = ("the flagged pattern is no longer in this file — showing the "
                    "head of it instead")
        return _code_section(f"{rel} — the lines in question", text, marks,
                             path=rel, note=note)

    async def whole_file():
        if refused or text is not None or not rel:
            return None
        return _note("File unavailable",
                     "The flagged file could not be read (deleted, renamed, or "
                     "binary). The git diff below may still show what happened.")

    async def modules():
        if trigger != "new_import" or not slug:
            return None
        return _modules_section(_project_dir(slug), detail.get("modules") or [])

    async def reach():
        """For code that can talk out: how far it could actually get."""
        if trigger != "network_call":
            return None
        pol = await egress.get_policy(db, slug or egress.GENERAL)
        hosts = pol.get("effective") or []
        return _facts("What this code could reach", [
            ["Egress mode", f"{pol.get('mode')} (from the {pol.get('source')} list)"],
            ["Hosts reachable", f"{len(hosts)} allowed"
             + (f" — {', '.join(map(str, hosts[:8]))}" if hosts else "")],
            ["Note", "this is the guest's policy: code running in the VM can only "
                     "reach these, and every attempt is logged on the Network tab. "
                     "Code run outside the guest is not gated by it."],
        ])

    await add(facts)
    await add(tripped)
    await add(modules)
    await add(code)
    await add(reach)
    await add(whole_file)
    if slug and rel:
        await add(lambda: _diff_section(slug, rel))
    rel_events = await _related(db, ev, path=rel)
    if path is not None and slug:
        await add(lambda: _files_section(_project_dir(slug), path, rel, event_ts,
                                         rel_events["flagged_paths"]))
        await add(lambda: _status_section(slug))
    await add(lambda: _related_table("Other alerts on this file",
                                     rel_events["same"], subject=rel or "this project"))
    return brief


# --- egress_anomaly ----------------------------------------------------------

async def _egress_board(db, ev, detail, add) -> dict:
    host = str(detail.get("host") or "")
    slug = ev.get("project_slug")
    kind = ("high_entropy" if "entropy" in detail else
            "volume_spike" if "bytes_out" in detail else
            "beacon_cadence" if "period_seconds" in detail else "unknown")
    brief = _brief(f"egress_{kind}", "Egress anomaly")
    event_ts = _ts(ev.get("created_at"))

    try:
        async with db.execute(
                "SELECT method, path, bytes_out, bytes_in, verdict, reason, created_at, "
                "conversation_id FROM egress_events WHERE host = ? "
                "ORDER BY id DESC LIMIT 40", (host,)) as cur:
            hits = [dict(r) for r in await cur.fetchall()]
    except Exception:                               # noqa: BLE001
        hits = []

    async def facts():
        rows = [["Host", host],
                ["Detector", kind.replace("_", " ")],
                ["Project", await _project_name(db, slug) or slug or "(no project)"],
                ["Detected", f"{_iso(event_ts) or ev.get('created_at')} ({_ago(event_ts)})"]]
        if kind == "high_entropy":
            rows.append(["Entropy", f"{detail.get('entropy')} bits/char",
                         f"threshold {detail.get('threshold')} — above this a name "
                         "is treated as machine-generated"])
        elif kind == "volume_spike":
            rows.append(["Bytes out", f"{_bytes(detail.get('bytes_out'))} "
                                      f"({detail.get('bytes_out')} bytes)"])
            rows.append(["Project baseline", _bytes(detail.get("baseline")),
                         "median of this project's other hosts"])
            rows.append(["Trigger", f"more than {detail.get('multiple')}× the baseline"])
        elif kind == "beacon_cadence":
            rows.append(["Interval", f"about {detail.get('period_seconds')}s between hits"])
            rows.append(["Regularity", f"cv {detail.get('cv')}",
                         "coefficient of variation — 0 is a metronome, real "
                         "traffic is well above it"])
            rows.append(["Hits counted", detail.get("hits")])
        rows.append(["Cut now",
                     "yes — the proxy refuses it and nftables drops its IPs"
                     if egress.is_cut(slug, host) else
                     "no — the cut is held in memory, so a backend restart since "
                     "the alert has cleared it. The host is reachable again unless "
                     "policy says otherwise."])
        try:
            pol = await egress.get_policy(db, slug or egress.GENERAL)
            rows.append(["Policy", f"{pol.get('mode')} (from the "
                                   f"{pol.get('source')} list)"])
            rows.append(["On the allowlist",
                         "yes — this host was approved at some point"
                         if host in (pol.get("effective") or []) else
                         "no — it is deny-by-default now"])
        except Exception:                           # noqa: BLE001
            pass
        convs = [c for c in dict.fromkeys(h["conversation_id"] for h in hits) if c]
        if convs:
            labels = [await _conv_label(db, c) for c in convs[:3]]
            rows.append(["Reached by", "; ".join(x for x in labels if x)])
        if hits:
            first, last = _ts(hits[-1]["created_at"]), _ts(hits[0]["created_at"])
            rows.append(["Traffic window", f"{_iso(first)} → {_iso(last)}"])
            rows.append(["Totals", f"{len(hits)} recent requests · "
                                   f"{_bytes(sum(h['bytes_out'] or 0 for h in hits))} out · "
                                   f"{_bytes(sum(h['bytes_in'] or 0 for h in hits))} in"])
        return _facts("The anomaly", rows)

    async def requests():
        rows = [[_iso(_ts(h["created_at"])), h["verdict"], h["method"] or "",
                 (h["path"] or "")[:160], _bytes(h["bytes_out"]), _bytes(h["bytes_in"]),
                 h["reason"] or ""] for h in hits]
        return _table("Requests to this host",
                      ["When", "Verdict", "Method", "Path", "Out", "In", "Reason"], rows,
                      note="what actually went over the wire, newest first "
                           "(paths are untrusted guest input)",
                      empty="no recorded requests — the anomaly fired on history "
                            "that has since been trimmed")

    async def neighbours():
        q = ("SELECT host, COUNT(*) n, SUM(bytes_out) o, SUM(bytes_in) i, "
             "MAX(created_at) last FROM egress_events WHERE verdict='allow' ")
        args: tuple = ()
        if slug:
            q += "AND project_slug = ? "
            args = (slug,)
        q += "GROUP BY host ORDER BY o DESC LIMIT 15"
        async with db.execute(q, args) as cur:
            rows = [[r["host"], r["n"], _bytes(r["o"]), _bytes(r["i"]),
                     _iso(_ts(r["last"]))] for r in await cur.fetchall()]
        return _table("Where this project's bytes go",
                      ["Host", "Requests", "Out", "In", "Last seen"], rows,
                      note="the baseline a volume spike is measured against")

    async def cadence():
        if kind != "beacon_cadence" or len(hits) < 3:
            return None
        times = [_ts(h["created_at"]) for h in hits]
        times = [t for t in times if t][:13]
        rows = []
        for newer, older in zip(times, times[1:]):
            rows.append([_iso(older), f"{newer - older:.1f}s"])
        return _table("Gaps between the last requests", ["Request at", "Gap to next"],
                      rows, note="a flat column is the tell")

    await add(facts)
    await add(requests)
    await add(cadence)
    await add(neighbours)
    rel_events = await _related(db, ev, host=host)
    await add(lambda: _related_table("Other alerts for this host",
                                     rel_events["same"], subject=host))
    return brief


# --- login_failed ------------------------------------------------------------

async def _login_board(db, ev, detail, add) -> dict:
    brief = _brief("login_failed", "Failed logins")
    who = str(detail.get("username") or "")
    peer = str(detail.get("peer") or "")
    event_ts = _ts(ev.get("created_at"))

    async def facts():
        async with db.execute("SELECT username FROM users") as cur:
            users = [r["username"] for r in await cur.fetchall()]
        real = who.lower() in {u.lower() for u in users}
        # the username is untrusted: escape LIKE wildcards so a name containing
        # % doesn't quietly count every burst on the instance
        pat = who.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with db.execute(
                "SELECT COUNT(*) n, MIN(created_at) first FROM security_events "
                r"WHERE kind='login_failed' AND summary LIKE ? ESCAPE '\'",
                (f"%'{pat}'%",)) as cur:
            r = await cur.fetchone()
        return _facts("The attempts", [
            ["Username tried", who or "(blank)"],
            ["Is that a real account",
             "YES — someone is guessing at an account that exists" if real
             else "no — no such user, which reads as untargeted scanning"],
            ["Attempts in this burst", detail.get("attempts")],
            ["From", peer, "taken from CF-Connecting-IP / X-Forwarded-For where "
                           "present — a hint only, never used for throttling"],
            ["Detected", f"{_iso(event_ts) or ev.get('created_at')} ({_ago(event_ts)})"],
            ["Bursts for this name", f"{r['n']} alert(s) since {_iso(_ts(r['first']))}"
             if r and r["n"] else None],
            ["Accounts on this instance", len(users)],
            ["Throttle", "each further attempt is delayed up to 8s; the account is "
                         "never locked out"],
        ])

    async def history():
        async with db.execute(
                "SELECT summary, created_at, acknowledged FROM security_events "
                "WHERE kind='login_failed' ORDER BY id DESC LIMIT 15") as cur:
            rows = [[_iso(_ts(r["created_at"])), r["summary"],
                     "seen" if r["acknowledged"] else "waiting"]
                    for r in await cur.fetchall()]
        return _table("Failed-login bursts", ["When", "Summary", "State"], rows,
                      note="every burst recorded, newest first — a cadence here "
                           "tells you scanning from noise")

    await add(facts)
    await add(history)
    return brief


# --- computeruse_auth --------------------------------------------------------

async def _cu_board(db, ev, detail, add) -> dict:
    brief = _brief("computeruse_auth", "Rejected pairing attempts")
    peer = str(detail.get("peer") or "")
    event_ts = _ts(ev.get("created_at"))

    async def facts():
        private = peer.startswith(("10.", "192.168.", "172.16.", "172.17.",
                                   "172.18.", "127.", "::1"))
        return _facts("The attempts", [
            ["From", peer],
            ["Looks like", "your LAN / loopback — most likely one of your own "
                           "clients with a stale token" if private
                           else "outside your LAN — treat as a probe"],
            ["Rejected attempts", detail.get("attempts"),
             "counted in a 5 minute window; one alert per burst"],
            ["Detected", f"{_iso(event_ts) or ev.get('created_at')} ({_ago(event_ts)})"],
            ["How that socket authenticates",
             "a pairing token, not a session cookie — so a bad token here is the "
             "whole failed handshake"],
        ])

    async def clients():
        from . import computeruse as cu
        rows = [[c.name, c.platform, c.id[:12],
                 _iso(c.connected_at), _ago(c.connected_at)]
                for c in cu.clients()]
        return _table("Computers connected right now",
                      ["Name", "Platform", "Client id", "Since", "Age"], rows,
                      note="if one of these went missing around the alert, the "
                           "rejected attempts are probably that client reconnecting",
                      empty="nothing is connected — so none of these attempts "
                            "succeeded")

    async def history():
        async with db.execute(
                "SELECT summary, created_at, acknowledged FROM security_events "
                "WHERE kind='computeruse_auth' ORDER BY id DESC LIMIT 12") as cur:
            rows = [[_iso(_ts(r["created_at"])), r["summary"],
                     "seen" if r["acknowledged"] else "waiting"]
                    for r in await cur.fetchall()]
        return _table("Earlier pairing bursts", ["When", "Summary", "State"], rows)

    await add(facts)
    await add(clients)
    await add(history)
    return brief


# --- fallback ----------------------------------------------------------------

async def _generic_board(db, ev, detail, add) -> dict:
    """An unknown kind still gets labelled facts and its neighbours — a new
    alert type is useful on day one without a GUI change."""
    kind = str(ev.get("kind") or "alert")

    async def facts():
        rows = [["Kind", kind], ["Severity", ev.get("severity")],
                ["Project", await _project_name(db, ev.get("project_slug"))
                 or ev.get("project_slug")],
                ["Raised", f"{_iso(_ts(ev.get('created_at')))} "
                           f"({_ago(_ts(ev.get('created_at')))})"]]
        for k, v in (detail or {}).items():
            if k == "note":
                continue
            rows.append([str(k).replace("_", " ").capitalize(), v])
        return _facts("The event", rows)

    await add(facts)
    if (detail or {}).get("note"):
        await add(lambda: _note("What this means", str(detail["note"])))
    rel_events = await _related(db, ev)
    await add(lambda: _related_table(f"Other {kind} alerts", rel_events["same"],
                                     subject="this kind"))
    return {"title": kind.replace("_", " ").capitalize(),
            "what": ev.get("summary") or "", "why": "", "checks": []}


_BOARDS = {"write_flag": _write_board, "egress_anomaly": _egress_board,
           "login_failed": _login_board, "computeruse_auth": _cu_board}


async def build_board(db: aiosqlite.Connection, ev: dict) -> dict:
    """The full board for one event. Never raises on bad data: a section that
    cannot be assembled becomes a visible note saying so, because a board that
    silently drops the diff is worse than one that admits it."""
    detail = ev.get("detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {"text": detail}
    if not isinstance(detail, dict):
        detail = {"value": detail}

    sections: list[dict] = []

    async def add(fn):
        try:
            s = fn()
            if hasattr(s, "__await__"):
                s = await s
            if s:
                sections.extend(s if isinstance(s, list) else [s])
        except Exception as e:                      # noqa: BLE001 — honest, not silent
            sections.append(_note("This part could not be assembled",
                                  f"{type(e).__name__}: {e}"))

    builder = _BOARDS.get(str(ev.get("kind") or ""), _generic_board)
    try:
        brief = await builder(db, ev, detail, add)
    except Exception as e:                          # noqa: BLE001
        brief = {"title": str(ev.get("kind") or "alert"), "what": "", "why": "",
                 "checks": []}
        sections.append(_note("This board could not be assembled",
                              f"{type(e).__name__}: {e}"))
    return {"event": ev, "detail": detail, **brief, "sections": sections}
