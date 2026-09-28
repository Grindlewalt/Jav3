"""Live navigation comparison: is a candidate model CLEARLY better than the
baseline (DeepSeek V4.1 Flash) at desktop navigation on a paired Mac?

    python -m scripts.nav_compare --server http://localhost:18000 \\
        --agents navigator,navigator-qwen --runs 3 --tasks scripts/nav_tasks.yaml \\
        [--only id,...] [--out DIR] [--desk-pid-file FILE] [--dry-run]

Runs ON the paired Mac (the one running clients/jav3-desk), against a Jav3
server reached over the operator's ssh tunnel. For every (agent, task, run):

  setup (local shell, inside ~/jav3-nav-scratch)  ->  one agent chat turn
  (POST /api/chat {"agent": slug} — the thread runs AS that AGENT.md, its
  model and exclusions; chat.py ChatRequest.agent)  ->  stream SSE, count
  desk_*/browser_* calls, stop the turn at max_steps  ->  check (local shell,
  inspects real state)  ->  cleanup  ->  record success, steps, wall, cost.

Presets: `navigator` (model "" = the server default, the baseline) and
`navigator-qwen` (openrouter/qwen/qwen3.8-27b) are created/updated through
POST/PUT /api/agents from the `agents:` block of the task file, so nothing is
hand-edited. Another candidate is one more entry there.

Safety watchdog (like the turn.py helper it replaces): abort the run, stop the
turn and SIGTERM the desk client — then end the whole session, since the desk
is gone — on desk_shell, desk_open of an app outside `allowed_apps` or a URL
other than the local fixture server, or any pointer action that lands in a
window owned by an app outside `allowed_apps`. The element list carries no
window titles (the client's AX walk emits only role/label/box), so the owner
is resolved on this Mac from CGWindowList at the click point; keyboard verbs
check the frontmost app. The `tool` event is published as the call is
dispatched, so like turn.py this is a fast brake, not a pre-execution gate —
the preset's exclusions (desk_shell is withheld) are the gate.

Verdict (the operator's bar: switch only if clearly better):
  gate  the candidate's grounding-probe row (GET /api/grounding): hit rate
        >= 0.953 and p95 <= 2500 ms
  live  success rate >= baseline + 15 points; on tasks whose success counts
        differ, candidate wins >= 3x baseline wins; $/success <= 2x baseline.

Pure pieces (task validation, watchdog, SSE parsing, scoring, verdict) take no
I/O so tests drive them directly; the network uses one httpx.Client that tests
replace with a MockTransport.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import yaml

# --- the navigator preset ------------------------------------------------------

# CO-STAR, like agents_api.DEFAULT_PROMPT. The navigation playbook
# (backend/navplaybook.py) is appended by the server on every turn offered
# desk_*/browser_* tools, so only the sub-agent specifics live here.
NAVIGATOR_PROMPT = """# Context
You are Navigator, a sub-agent that operates a connected computer (desk_*
tools) or a browser tab (browser_* tools) for the operator or a head agent.
You get one scoped sub-goal, e.g. "in TextEdit, type X and save it as Y".
Nobody answers questions mid-run: decide and act on the brief.

# Objective
Reach the sub-goal's end state on the real screen, verified, in as few
actions as possible. Stay inside the apps, windows, folders and pages the
brief names. Never open other apps, never use a shell, never send, buy,
delete outside the brief, or change settings. If the goal cannot be reached
(locked screen, missing element, a prompt that needs the operator), stop and
say exactly why.

# Style
Screenshot first and act by element id; zoom (region) before any small
target; prefer the keyboard for menus, dialogs, dropdowns and saving (cmd+s,
cmd+shift+g in a save panel). Read the changed: line after every action: if
nothing changed, do not repeat the click — focus the right field, zoom, or
use the keyboard. When two elements share a label, pick by the surrounding
section, never the first match. Verify the end state on screen before
reporting done.

# Tone
Terse and factual.

# Audience
A harness or head agent that parses your last message.

# Response
End with exactly one line of JSON and nothing after it:
{"done": true|false, "evidence": "<what on screen proves it, or why not>", "steps": <actions taken>}
"""

# Registry names a navigator keeps; everything else is excluded (the
# exclusion model is subtractive — agents_api.FIELD_DEFAULTS). desk_shell is
# desk_* but withheld: the watchdog aborts on it, so offering it only burns
# runs. There is no dedicated result/finish tool in the registry: the result
# is the final message's JSON line.
KEEP_PREFIXES = ("desk_", "browser_")
KEEP_NAMES = {"web_read"}
ALWAYS_EXCLUDE = {"desk_shell"}
# the lean context a narrow worker gets (agents_run.TEMP_LEAN_EXCLUDE)
LEAN_CONTEXT = ["soul.md", "standing-memory", "user.md", "all-projects.md",
                "agents-index", "secrets-index"]


def keep_tool(name: str) -> bool:
    if name in ALWAYS_EXCLUDE:
        return False
    return name.startswith(KEEP_PREFIXES) or name in KEEP_NAMES


def preset_body(slug: str, spec: dict, tool_names: Iterable[str]) -> dict:
    """The SaveAgent body (PUT /api/agents/{slug}) for one preset."""
    names = sorted(set(tool_names))
    return {
        "name": slug,
        "description": spec.get("description")
        or "Scoped desktop/web navigation sub-goals; reports {done, evidence, steps}.",
        "model": spec.get("model", "") or "",
        "base_url": spec.get("base_url", "") or "",
        "own_memory": False,
        "context_exclude": list(spec.get("context_exclude", LEAN_CONTEXT)),
        "tools_exclude": [n for n in names if not keep_tool(n)],
        "skills_exclude": [],
        "max_iterations": int(spec.get("max_iterations", 40)),
        "project": "",
        "prompt": spec.get("prompt") or NAVIGATOR_PROMPT,
    }


# --- task file -----------------------------------------------------------------

REQUIRED_TASK_KEYS = ("id", "prompt", "check")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
# nothing in a task may touch these (the operator's scope rule)
_FORBIDDEN = re.compile(
    r"\bsudo\b|\brm\s|\bdefaults\s+write\b|\bMail\b|\bMessages\b|System Settings|"
    r"System Preferences|\blaunchctl\b|\bcurl\b|\bssh\b|\bkillall\b", re.I)
_APP_REFS = (re.compile(r'\bopen\s+-a\s+"?([A-Za-z][A-Za-z0-9]*)'),
             re.compile(r'\b(?:application|app)\s+\\?"([^"\\]+)'))
READ_ONLY_APPS = {"System Events"}   # osascript reads (frontmost app) only


class TaskError(ValueError):
    pass


def _cmds(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list) and all(isinstance(c, str) for c in v):
        return list(v)
    raise TaskError(f"expected a command or a list of commands, got {v!r}")


def load_tasks(path: str | Path) -> dict:
    return validate_spec(yaml.safe_load(Path(path).read_text()))


def validate_spec(spec: dict) -> dict:
    """Normalise and check the task file; raises TaskError naming the first
    problem. Returns {scratch, port, allowed_apps, agents, fixtures, tasks}."""
    if not isinstance(spec, dict):
        raise TaskError("task file must be a mapping")
    allowed = spec.get("allowed_apps") or []
    if not allowed:
        raise TaskError("allowed_apps is required")
    scratch = str(spec.get("scratch") or "~/jav3-nav-scratch")
    if "jav3-nav-scratch" not in scratch:
        raise TaskError("scratch must be the dedicated jav3-nav-scratch folder")
    fixtures = spec.get("fixtures") or {}
    for name in fixtures:
        if "/" in name or name.startswith("."):
            raise TaskError(f"fixture name {name!r} must be a plain file name")
    agents = spec.get("agents") or {}
    tasks, seen = [], set()
    for raw in spec.get("tasks") or []:
        if not isinstance(raw, dict):
            raise TaskError(f"task must be a mapping: {raw!r}")
        for k in REQUIRED_TASK_KEYS:
            if not raw.get(k):
                raise TaskError(f"task {raw.get('id', '?')}: missing {k}")
        tid = raw["id"]
        if not _ID_RE.match(tid):
            raise TaskError(f"task id {tid!r} must be lowercase letters, digits, dashes")
        if tid in seen:
            raise TaskError(f"duplicate task id {tid}")
        seen.add(tid)
        t = {"id": tid, "prompt": raw["prompt"].strip(),
             "setup": _cmds(raw.get("setup")), "check": _cmds(raw.get("check")),
             "cleanup": _cmds(raw.get("cleanup")),
             "max_steps": int(raw.get("max_steps", 30)),
             "tags": list(raw.get("tags") or [])}
        if not 1 <= t["max_steps"] <= 60:
            raise TaskError(f"task {tid}: max_steps out of range")
        for cmd in t["setup"] + t["check"] + t["cleanup"]:
            if _FORBIDDEN.search(cmd):
                raise TaskError(f"task {tid}: forbidden command: {cmd[:80]}")
            for app in (x for rx in _APP_REFS for x in rx.findall(cmd)):
                app = app.strip()
                if app not in allowed and app not in READ_ONLY_APPS:
                    raise TaskError(f"task {tid}: app {app!r} is not in allowed_apps")
        tasks.append(t)
    if not tasks:
        raise TaskError("no tasks")
    return {"scratch": scratch, "port": int(spec.get("fixture_port", 8765)),
            "allowed_apps": list(allowed), "agents": agents,
            "fixtures": fixtures, "tasks": tasks}


def task_env(spec: dict, task: dict) -> dict:
    scratch = os.path.expanduser(spec["scratch"])
    return {"SCRATCH": scratch, "TASK_DIR": f"{scratch}/{task['id']}",
            "WEB": f"{scratch}/web", "PORT": str(spec["port"]),
            "BASE_URL": f"http://127.0.0.1:{spec['port']}"}


def render_prompt(task: dict, env: dict) -> str:
    out = task["prompt"]
    for k, v in env.items():
        out = out.replace("{" + k + "}", v)
    return out


# --- SSE -----------------------------------------------------------------------

def parse_sse_line(line: str) -> dict | None:
    if not line.startswith("data:"):
        return None
    try:
        ev = json.loads(line[5:])
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


_RESULT_JSON = re.compile(r"\{[^{}]*\"done\"[^{}]*\}")


def parse_claim(final: str) -> dict | None:
    """The navigator's closing {done, evidence, steps} line, if it wrote one."""
    for m in reversed(_RESULT_JSON.findall(final or "")):
        try:
            return json.loads(m)
        except ValueError:
            continue
    return None


# --- watchdog ------------------------------------------------------------------

_SCREEN = re.compile(r"^screen (\d+)x(\d+)", re.M)
_ZOOM = re.compile(r"^zoomed region (\d+),(\d+) (\d+)x(\d+).*?shown at (\d+)x(\d+)", re.M)
_ELEMENT = re.compile(r"^\s*\[(\d+)\] \S+ .*? @ (-?\d+),(-?\d+) \d+x\d+", re.M)
_AT = re.compile(r"\bat (-?\d+),(-?\d+)")
POINTER_TOOLS = {"desk_click", "desk_move", "desk_scroll", "desk_drag"}
KEY_TOOLS = {"desk_type", "desk_key"}


class Abort(Exception):
    """The watchdog tripped: stop the turn, stop the desk client, stop all."""


@dataclass
class Frame:
    full_w: int = 0            # the last full-frame image width (image px)
    full_h: int = 0
    zoom: tuple | None = None  # (rx, ry, rw, rh, shown_w, shown_h) or None
    elements: dict = field(default_factory=dict)   # id -> (x, y) image px

    def to_full(self, x: float, y: float) -> tuple[float, float]:
        """A point of the latest image in full-frame image px."""
        if self.zoom:
            rx, ry, rw, rh, sw, sh = self.zoom
            return rx + x * rw / sw, ry + y * rh / sh
        return x, y


class Watchdog:
    """Pure: feed it tool events and results; it raises Abort. `owner_at`
    (full-frame image px, frame) -> owning app name or None, and `front_app`
    () -> frontmost app name or None, are injected (Mac probes live, fakes in
    tests); None from either means unknown and never aborts."""

    def __init__(self, allowed_apps: Iterable[str], allowed_url_prefix: str,
                 owner_at: Callable | None = None, front_app: Callable | None = None):
        self.allowed = {a.lower() for a in allowed_apps}
        self.url_prefix = allowed_url_prefix
        self.owner_at = owner_at or (lambda x, y, f: None)
        self.front_app = front_app or (lambda: None)
        self.frame = Frame()

    def _app_ok(self, app: str | None) -> bool:
        return app is None or app.lower() in self.allowed

    def _check_point(self, x, y, what: str) -> None:
        fx, fy = self.frame.to_full(float(x), float(y))
        app = self.owner_at(fx, fy, self.frame)
        if not self._app_ok(app):
            raise Abort(f"{what} lands in a {app} window at {int(fx)},{int(fy)}")

    def on_tool(self, name: str, args: dict) -> None:
        args = args or {}
        if name == "desk_shell":
            raise Abort("desk_shell called")
        if name == "desk_open":
            app, url = args.get("app"), args.get("url")
            if app and app.lower() not in self.allowed:
                raise Abort(f"desk_open of app {app!r}")
            if url and not str(url).startswith(self.url_prefix):
                raise Abort(f"desk_open of url {str(url)[:80]!r}")
            return
        if name in POINTER_TOOLS:
            pts = []
            if "element" in args and args["element"] is not None:
                try:
                    el = self.frame.elements.get(int(args["element"]))
                except (TypeError, ValueError):
                    el = None
                if el:
                    pts.append(el)
            elif args.get("x") is not None and args.get("y") is not None:
                pts.append((args["x"], args["y"]))
            if name == "desk_drag" and args.get("to_x") is not None:
                pts.append((args["to_x"], args.get("to_y", 0)))
            for x, y in pts:
                self._check_point(x, y, name)
            if not pts:   # target= (resolved server side): the front app, then the result
                self._front_check(name)
        elif name in KEY_TOOLS:
            self._front_check(name)

    def _front_check(self, name: str) -> None:
        app = self.front_app()
        if not self._app_ok(app):
            raise Abort(f"{name} while {app} is the frontmost app")

    def on_result(self, name: str, text: str) -> None:
        """Track the latest frame; a target= click is checked after the fact
        from the "clicked ... at X,Y" line."""
        if not name.startswith("desk_") or not text:
            return
        z, s = _ZOOM.search(text), _SCREEN.search(text)
        if name == "desk_click" and self.frame.full_w:
            first = text.splitlines()[0] if text else ""
            m = _AT.search(first)
            if m and first.startswith("clicked"):
                self._check_point(m.group(1), m.group(2), "desk_click (resolved)")
        if z:
            self.frame.zoom = tuple(int(v) for v in z.groups())
        elif s:
            self.frame.full_w, self.frame.full_h = int(s.group(1)), int(s.group(2))
            self.frame.zoom = None
        if z or s:
            self.frame.elements = {int(i): (int(x), int(y))
                                   for i, x, y in _ELEMENT.findall(text)}


# --- Mac probes (live only) ----------------------------------------------------

_JXA_OWNER = r"""
ObjC.import('CoreGraphics'); ObjC.import('AppKit');
function run(argv) {
  var fx = parseFloat(argv[0]), fy = parseFloat(argv[1]), iw = parseFloat(argv[2]);
  var sw = $.NSScreen.mainScreen.frame.size.width;
  var x = fx * sw / iw, y = fy * sw / iw;
  var ws = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1 | 16, 0)));
  for (var i = 0; i < ws.length; i++) {
    var w = ws[i], b = w.kCGWindowBounds;
    if (!b) continue;
    if (x >= b.X && x < b.X + b.Width && y >= b.Y && y < b.Y + b.Height) {
      if (w.kCGWindowLayer === 0) return w.kCGWindowOwnerName;
      if (w.kCGWindowLayer === 24 || w.kCGWindowLayer === 25) return 'menubar';
    }
  }
  return 'desktop';
}
"""


def _osa(args: list[str], timeout: float = 5.0) -> str | None:
    try:
        r = subprocess.run(["osascript", *args], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def mac_front_app() -> str | None:
    return _osa(["-e", 'tell application "System Events" to get name of first '
                 'process whose frontmost is true'])


def mac_owner_at(fx: float, fy: float, frame: Frame) -> str | None:
    """Owner app of the topmost normal window under a full-frame image point,
    on the main display (the harness assumes one display). The menu bar and
    the bare desktop count as the frontmost app's / Finder's."""
    if not frame.full_w:
        return None
    got = _osa(["-l", "JavaScript", "-e", _JXA_OWNER, str(fx), str(fy),
                str(frame.full_w)])
    if got == "menubar":
        return mac_front_app()
    if got == "desktop":
        return "Finder"
    return got


def stop_desk(pid_file: str | None) -> str:
    if pid_file and Path(pid_file).is_file():
        try:
            os.kill(int(Path(pid_file).read_text().strip()), signal.SIGTERM)
            return f"SIGTERM to pid in {pid_file}"
        except (OSError, ValueError) as e:
            return f"could not signal pid in {pid_file}: {e}"
    subprocess.run(["pkill", "-TERM", "-f", "jav3-desk"], capture_output=True)
    return "pkill -f jav3-desk"


# --- local shell (setup / check / cleanup) --------------------------------------

def run_local(cmds: list[str], env: dict, timeout: float = 60.0) -> tuple[bool, str]:
    """Run each command with bash in the task dir; stop at the first failure."""
    full = {**os.environ, **env}
    cwd = env.get("TASK_DIR") if Path(env.get("TASK_DIR", "")).is_dir() else None
    for cmd in cmds:
        try:
            r = subprocess.run(["bash", "-c", cmd], env=full, cwd=cwd,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timeout: {cmd[:80]}"
        if r.returncode != 0:
            return False, f"exit {r.returncode}: {cmd[:80]}: {(r.stderr or r.stdout)[-300:]}"
    return True, ""


def prepare_scratch(spec: dict) -> Path:
    scratch = Path(os.path.expanduser(spec["scratch"]))
    web = scratch / "web"
    web.mkdir(parents=True, exist_ok=True)
    for name, body in spec["fixtures"].items():
        (web / name).write_text(body)
    return scratch


def reset_task_dir(env: dict) -> None:
    import shutil
    scratch = Path(env["SCRATCH"]).resolve()
    d = Path(env["TASK_DIR"]).resolve()
    if d.parent != scratch or "jav3-nav-scratch" not in str(scratch):
        raise RuntimeError(f"refusing to reset {d}")
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)


# --- one run -------------------------------------------------------------------

@dataclass
class RunResult:
    agent: str
    task: str
    run: int
    success: bool = False
    status: str = "ok"       # ok | max_steps | timeout | model_error | infra_error | aborted | setup_failed
    steps: int = 0
    wall_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""
    conversation_id: int | None = None
    claim: dict | None = None
    first_error: str = ""
    check_detail: str = ""
    tools: list = field(default_factory=list)


class Api:
    def __init__(self, client, server: str):
        self.c, self.base = client, server.rstrip("/")

    def get(self, path, **kw):
        return self.c.get(self.base + path, **kw)

    def post(self, path, **kw):
        return self.c.post(self.base + path, **kw)

    def put(self, path, **kw):
        return self.c.put(self.base + path, **kw)


def ensure_presets(api: Api, agents: dict, slugs: list[str]) -> list[str]:
    """Create/update each named preset through the agents API. Returns lines
    for the log."""
    r = api.get("/api/tools")
    r.raise_for_status()
    names = [t["name"] for t in r.json().get("tools", [])]
    out = []
    for slug in slugs:
        if slug not in agents:
            raise TaskError(f"agent {slug!r} has no entry under agents: in the task file")
        if api.get(f"/api/agents/{slug}").status_code == 404:
            c = api.post("/api/agents", json={"name": slug})
            c.raise_for_status()
            if c.json().get("slug") != slug:
                raise TaskError(f"agent name {slug!r} slugified to {c.json().get('slug')!r}")
        body = preset_body(slug, agents[slug] or {}, names)
        api.put(f"/api/agents/{slug}", json=body).raise_for_status()
        out.append(f"preset {slug}: model={body['model'] or '(default)'}, "
                   f"{len(names) - len(body['tools_exclude'])} tools kept")
    return out


def run_turn(api: Api, slug: str, prompt: str, max_steps: int, watchdog: Watchdog,
             res: RunResult, timeout_s: float = 600.0) -> None:
    """One agent chat turn, streamed. Fills res; raises Abort on the watchdog."""
    t0 = time.monotonic()
    body = {"message": prompt, "agent": slug, "permission_mode": "yolo"}
    cid = None
    try:
        with api.c.stream("POST", api.base + "/api/chat", json=body,
                          timeout=timeout_s) as r:
            if r.status_code != 200:
                r.read()
                res.status, res.first_error = "infra_error", f"POST /api/chat {r.status_code}: {r.text[:200]}"
                return
            for line in r.iter_lines():
                ev = parse_sse_line(line)
                if ev is None:
                    continue
                ty = ev.get("type")
                if ev.get("conversation_id") and cid is None:
                    cid = res.conversation_id = int(ev["conversation_id"])
                if ty == "start":
                    res.model = ev.get("model") or ""
                elif ty == "tool":
                    name, args = ev.get("name", ""), ev.get("args") or {}
                    res.tools.append({"name": name, "args": _small(args)})
                    if name.startswith(("desk_", "browser_")):
                        res.steps += 1
                    try:
                        watchdog.on_tool(name, args)
                    except Abort:
                        _stop(api, cid)
                        raise
                    if res.steps > max_steps:
                        res.status = "max_steps"
                        _stop(api, cid)
                        break
                elif ty == "tool_result":
                    ok = ev.get("ok", True)
                    text = ev.get("result") or ""
                    if not ok and not res.first_error:
                        res.first_error = f"{ev.get('name')}: {text[:200]}"
                    try:
                        watchdog.on_result(ev.get("name", ""), text)
                    except Abort:
                        _stop(api, cid)
                        raise
                elif ty == "final":
                    res.claim = parse_claim(str(ev.get("content") or ""))
                    break
                elif ty == "error":
                    res.status = "model_error"
                    res.first_error = res.first_error or str(ev.get("message", ""))[:300]
                    break
                if time.monotonic() - t0 > timeout_s:
                    res.status = "timeout"
                    _stop(api, cid)
                    break
    except Abort:
        raise
    except Exception as e:  # noqa: BLE001 — a dropped tunnel is a recorded infra error
        res.status = res.status if res.status != "ok" else "infra_error"
        res.first_error = res.first_error or f"{type(e).__name__}: {e}"[:300]
    finally:
        res.wall_s = round(time.monotonic() - t0, 2)


def _small(args: dict) -> dict:
    return {k: (v[:120] if isinstance(v, str) else v) for k, v in (args or {}).items()}


def _stop(api: Api, cid) -> None:
    if cid is None:
        return
    try:
        api.post(f"/api/chat/{cid}/stop")
    except Exception:  # noqa: BLE001
        pass


def fetch_usage(api: Api, res: RunResult) -> None:
    if res.conversation_id is None:
        return
    for _ in range(3):   # the turn's last ledger rows land just after `final`
        try:
            r = api.get(f"/api/conversations/{res.conversation_id}/info")
            if r.status_code == 200:
                j = r.json()
                res.input_tokens = int(j.get("input_tokens") or 0)
                res.output_tokens = int(j.get("output_tokens") or 0)
                res.cost_usd = float(j.get("cost_usd") or 0.0)
                res.model = j.get("model") or res.model
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0)


# --- scoring -------------------------------------------------------------------

BAR = {"hit_rate": 0.953, "p95_ms": 2500, "points": 0.15, "win_ratio": 3.0,
       "cost_ratio": 2.0}


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, math.ceil(q * len(xs)) - 1))
    return xs[k]


def scored(runs: list[dict]) -> list[dict]:
    """Runs that count: infrastructure failures (the tunnel, a 5xx before the
    turn started) and setup failures say nothing about the model."""
    return [r for r in runs if r["status"] not in ("infra_error", "setup_failed")]


def summarize(runs: list[dict]) -> dict:
    rs = scored(runs)
    n, ok = len(rs), sum(1 for r in rs if r["success"])
    cost = sum(r["cost_usd"] for r in rs)
    walls = [r["wall_s"] for r in rs]
    return {"runs": n, "excluded": len(runs) - n, "successes": ok,
            "success_rate": ok / n if n else 0.0,
            "mean_steps": statistics.fmean([r["steps"] for r in rs]) if rs else 0.0,
            "p50_wall": _pct(walls, 0.5), "p95_wall": _pct(walls, 0.95),
            "cost": cost, "cost_per_task": cost / n if n else 0.0,
            "cost_per_success": cost / ok if ok else math.inf}


def per_task(runs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in scored(runs):
        out[r["task"]] = out.get(r["task"], 0) + (1 if r["success"] else 0)
    return out


def verdict(base_runs: list[dict], cand_runs: list[dict],
            probe_row: dict | None, bar: dict = BAR) -> dict:
    """The pairwise decision. Every criterion is reported; `switch` only when
    all pass (an unmeasured gate or unpriced cost is a fail, not a pass)."""
    b, c = summarize(base_runs), summarize(cand_runs)
    checks = {}
    if probe_row is None:
        checks["gate"] = (False, "no grounding-probe row for the candidate model "
                                 "(run scripts/grounding_probe --models <id>)")
    else:
        hr, p95 = float(probe_row.get("hit_rate") or 0), float(probe_row.get("p95_ms") or 1e9)
        checks["gate"] = (hr >= bar["hit_rate"] and p95 <= bar["p95_ms"],
                          f"hit {hr:.3f} (>= {bar['hit_rate']}), p95 {p95:.0f} ms "
                          f"(<= {bar['p95_ms']})")
    delta = c["success_rate"] - b["success_rate"]
    checks["points"] = (delta >= bar["points"] - 1e-9,
                        f"{c['success_rate']:.1%} vs {b['success_rate']:.1%} "
                        f"= {delta * 100:+.1f} pts (>= +{bar['points'] * 100:.0f})")
    bt, ct = per_task(base_runs), per_task(cand_runs)
    cw = sum(1 for t in set(bt) & set(ct) if ct[t] > bt[t])
    bw = sum(1 for t in set(bt) & set(ct) if bt[t] > ct[t])
    ratio = math.inf if bw == 0 and cw > 0 else (cw / bw if bw else 0.0)
    checks["wins"] = (ratio >= bar["win_ratio"],
                      f"candidate wins {cw}, baseline wins {bw} on differing tasks "
                      f"(need >= {bar['win_ratio']:.0f}:1)")
    bc, cc = b["cost_per_success"], c["cost_per_success"]
    if c["successes"] and cc == 0.0 and c["runs"]:
        checks["cost"] = (False, "candidate cost is $0 — its model is unpriced in "
                                 "the catalogue, so the cost ratio is unmeasured")
    elif math.isinf(cc):
        checks["cost"] = (False, "candidate has no successes")
    elif math.isinf(bc):
        checks["cost"] = (True, f"baseline has no successes; candidate ${cc:.4f}/success")
    else:
        lim = bar["cost_ratio"] * bc
        checks["cost"] = (cc <= lim + 1e-12, f"${cc:.4f} vs ${bc:.4f} per success "
                                             f"(<= {bar['cost_ratio']:.0f}x = ${lim:.4f})")
    switch = all(ok for ok, _ in checks.values())
    return {"switch": switch, "baseline": b, "candidate": c,
            "checks": {k: {"pass": ok, "detail": d} for k, (ok, d) in checks.items()}}


def fmt_money(x: float) -> str:
    return "n/a" if math.isinf(x) else f"${x:.4f}"


def report(by_agent: dict[str, list[dict]], baseline: str,
           probes: dict[str, dict | None]) -> str:
    lines = ["| agent | runs | success | mean steps | p50 wall | p95 wall | $/task | $/success |",
             "|---|---|---|---|---|---|---|---|"]
    for slug, runs in by_agent.items():
        s = summarize(runs)
        lines.append(f"| {slug} | {s['runs']}" + (f" (+{s['excluded']} excl.)" if s["excluded"] else "")
                     + f" | {s['success_rate']:.1%} | {s['mean_steps']:.1f} | "
                     f"{s['p50_wall']:.1f}s | {s['p95_wall']:.1f}s | "
                     f"{fmt_money(s['cost_per_task'])} | {fmt_money(s['cost_per_success'])} |")
    for slug, runs in by_agent.items():
        if slug == baseline:
            continue
        v = verdict(by_agent[baseline], runs, probes.get(slug))
        lines += ["", f"**{slug} vs {baseline}: "
                  + ("SWITCH — clearly better on every criterion" if v["switch"]
                     else "KEEP the baseline") + "**"]
        for k, c in v["checks"].items():
            lines.append(f"- {k}: {'PASS' if c['pass'] else 'FAIL'} — {c['detail']}")
    return "\n".join(lines)


def probe_row(api: Api, model: str) -> dict | None:
    """The grounding model finder's row for `model` (GET /api/grounding)."""
    if not model:
        return None
    try:
        r = api.get("/api/grounding")
        rows = r.json().get("ranking") or [] if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        return None
    return next((row for row in rows if row.get("model") == model), None)


# --- main ----------------------------------------------------------------------

def session_cookie() -> str:
    s = json.load(open(Path.home() / ".config/jav3/credentials.json"))["session"]
    return "jarvis_token=" + (s[8:] if s.startswith("session:") else s)


def plan(spec: dict, agents: list[str], runs: int, only: list[str] | None) -> list[tuple]:
    tasks = [t for t in spec["tasks"] if not only or t["id"] in only]
    if only:
        missing = set(only) - {t["id"] for t in tasks}
        if missing:
            raise TaskError(f"unknown task ids: {', '.join(sorted(missing))}")
    # interleave agents per (task, run) so drift in the Mac's state or the
    # provider's latency hits both sides alike
    return [(a, t, i) for i in range(runs) for t in tasks for a in agents]


class FixtureServer:
    def __init__(self, directory: Path, port: int):
        self.dir, self.port, self.p = directory, port, None

    def __enter__(self):
        self.p = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port), "--bind", "127.0.0.1",
             "--directory", str(self.dir)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        return self

    def __exit__(self, *exc):
        if self.p:
            self.p.terminate()


def main(argv: list[str] | None = None, *, client=None, out=print,
         owner_at=None, front_app=None, local=run_local) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--server", default="http://localhost:18000")
    ap.add_argument("--agents", default="navigator,navigator-qwen",
                    help="comma list; the FIRST is the baseline")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tasks", default=str(Path(__file__).with_name("nav_tasks.yaml")))
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default="nav-compare-out")
    ap.add_argument("--desk-pid-file", default=None)
    ap.add_argument("--run-timeout", type=float, default=600.0)
    ap.add_argument("--no-presets", action="store_true",
                    help="use the presets as they are on the server")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    spec = load_tasks(a.tasks)
    agents = [s.strip() for s in a.agents.split(",") if s.strip()]
    only = [s.strip() for s in a.only.split(",") if s.strip()] or None
    todo = plan(spec, agents, a.runs, only)
    if a.dry_run:
        out(f"{len(todo)} runs: {len(agents)} agents x {len(todo) // max(1, len(agents) * a.runs)} "
            f"tasks x {a.runs} runs (baseline: {agents[0]})")
        for slug in agents:
            m = (spec["agents"].get(slug) or {}).get("model", "")
            out(f"  agent {slug}: model={m or '(server default)'}")
        seen = set()
        for _, t, _ in todo:
            if t["id"] not in seen:
                seen.add(t["id"])
                out(f"  {t['id']:<28} max_steps={t['max_steps']:<3} {' '.join(t['tags'])}")
        return 0

    import httpx
    own = client is None
    client = client or httpx.Client(headers={"Cookie": session_cookie()},
                                    timeout=httpx.Timeout(30.0, read=a.run_timeout))
    api = Api(client, a.server)
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    by_agent: dict[str, list[dict]] = {s: [] for s in agents}
    try:
        if not a.no_presets:
            for line in ensure_presets(api, spec["agents"], agents):
                out(line)
        probes = {s: probe_row(api, (spec["agents"].get(s) or {}).get("model", ""))
                  for s in agents[1:]}
        scratch = prepare_scratch(spec)
        url_prefix = f"http://127.0.0.1:{spec['port']}/"
        with FixtureServer(scratch / "web", spec["port"]):
            for slug, task, i in todo:
                env = task_env(spec, task)
                res = RunResult(agent=slug, task=task["id"], run=i)
                wd = Watchdog(spec["allowed_apps"], url_prefix,
                              owner_at=owner_at or mac_owner_at,
                              front_app=front_app or mac_front_app)
                aborted = None
                try:
                    reset_task_dir(env)
                    ok, why = local(task["setup"], env)
                    if not ok:
                        res.status, res.first_error = "setup_failed", why
                    else:
                        run_turn(api, slug, render_prompt(task, env), task["max_steps"],
                                 wd, res, a.run_timeout)
                        fetch_usage(api, res)
                        if res.status not in ("infra_error",):
                            ok, why = local(task["check"], env)
                            res.success = ok and res.status in ("ok",)
                            res.check_detail = why
                except Abort as e:
                    aborted = str(e)
                    res.status, res.first_error = "aborted", f"WATCHDOG: {e}"
                    out(f"!!! WATCHDOG: {e} — {stop_desk(a.desk_pid_file)}")
                finally:
                    local(task["cleanup"], env)
                by_agent[slug].append(asdict(res))
                (outdir / f"{slug}__{task['id']}__{i}.json").write_text(
                    json.dumps(asdict(res), indent=1))
                out(f"{slug:<16} {task['id']:<28} run {i} "
                    f"{'OK  ' if res.success else 'FAIL'} {res.status:<12} "
                    f"steps {res.steps:>2} {res.wall_s:6.1f}s ${res.cost_usd:.4f}"
                    + (f"  {res.first_error[:80]}" if res.first_error and not res.success else ""))
                if aborted:
                    break
        text = report(by_agent, agents[0], probes)
        (outdir / "report.md").write_text(text + "\n")
        out("")
        out(text)
        return 0
    finally:
        if own:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
