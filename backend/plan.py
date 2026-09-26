"""The explicit orchestrator: dump -> checklist -> agents that talk.

The funnel (orchestrator.py) is free-form and recursive: a head decides on the
fly how to split work, children decide again, and the operator sees the tree
only as it happens. This is the other shape, the one the operator asked for by
name: a big "dump" (spec, notes, a long ask) becomes an EXPLICIT, ORDERED
checklist first — persisted, readable and editable before a single agent runs —
and a deterministic host-side runner then walks it, the way research.py walks
its scout/reader/synthesize pipeline rather than free-looping.

Three durable facts and where they live:

* The checklist is a plain file, `projects/<slug>/.plan.json`, written through
  `writes.apply_write` like every other project file (secret-value refusal
  included: a dump that contains a real key is refused, not persisted). The GUI
  and the runner read and edit the same file under one in-process lock, so an
  operator marking an item done while it runs is a normal edit the runner
  picks up on its next tick.
* Each item's work is an ordinary headless agent run (agents_run._run_headless):
  a `conversations` row filed under the run's `job_id` with the plan's head as
  its parent, so the Runs tree, the Jobs view and Agent Outputs all see it with
  no new machinery. A named `assignee` runs AS that agent (its AGENT.md leads);
  an unassigned item runs as a lean temporary worker.
* Siblings talk through agentmsg (WP5). A running item is addressable by its
  conversation id AND by `item:<id>`, and `send_message(to="?")` lists running
  items that way, so a model never has to know a conversation id. A message to
  an item that has not started yet is kept as a note and lands in that item's
  brief when it spawns.

Completion is structured, never inferred from prose: an item calls the
`plan_report` tool (done / failed / blocked + summary); a fenced ```json block
in its final reply is accepted as the fallback; anything else is a failed
attempt. Failed items retry up to `attempts_max`; a quiet item (no tool call or
message for `plan_stall_seconds`) is nudged by message, then re-spawned once;
an item whose dependency failed is blocked, not attempted. One Budget covers
the whole run, and the run is a detached task: it survives the client that
started it going away, and `stop_run` cancels it.
"""
import asyncio
import contextlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from . import bus, orchestrator, runtime, writes
from .agent import budget as budget_mod
from .agent.budget import BudgetExceeded
from .agent.model import complete_text, confirm_peak
from .config import settings
from .db import get_db, launcher, open_conversation
from .memory import agents_index

PLAN_FILE = ".plan.json"
VERSION = 1

STATUSES = ("todo", "running", "blocked", "done", "failed", "skipped")
# what an operator edit may set directly (the runner owns `running`/`blocked`)
OPERATOR_STATUSES = ("todo", "done", "skipped", "failed", "blocked")
SETTLED = ("done", "skipped")            # satisfies a dependency
DEP_BLOCK = "dependency "                # last_error prefix of a runner-made block
REPORT_STATUSES = ("done", "failed", "blocked")

# the funnel's caps are the plan's caps: no more items than nodes, no more
# concurrent items than fan-out, no more spawns per run than nodes
MAX_ITEMS = orchestrator.MAX_NODES
MAX_CONCURRENT_CAP = orchestrator.MAX_FANOUT
MAX_SPAWNS = orchestrator.MAX_NODES
MAX_ATTEMPTS_CAP = 5
PLANNER_DUMP_CHARS = 40_000
PLANNER_FILE_CHARS = 12_000
SUMMARY_CHARS = 2_000

_locks: dict[str, asyncio.Lock] = {}
_runs: dict[str, asyncio.Task] = {}          # project slug -> the detached runner
_live_items: dict[int, dict] = {}            # conversation id -> {project, item_id, title}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the file ----------------------------------------------------------------

def empty_plan(*, title: str = "", dump: str = "") -> dict:
    return {"version": VERSION, "title": title[:120], "dump": dump,
            "created_at": _now(), "updated_at": _now(), "status": "draft",
            "job_id": None, "root_id": None, "next_id": 1,
            "attempts_max": settings.plan_attempts_max,
            "max_concurrent": settings.plan_max_concurrent,
            "max_iterations": 0, "items": []}


def new_item(plan: dict, *, title: str, brief: str = "", depends_on=(),
             assignee: str | None = None, id_: str | None = None,
             model: str | None = None) -> dict:
    """A fresh item. Without `id_` it takes the plan's next counter; with one
    (a file being re-read, a PUT naming its items) the counter is untouched —
    normalise() keeps the counter above every explicit numeric id.

    `model` (provider/model) is set only where the operator explicitly named
    the model for this work (see plan_from_dump); None runs the item on the
    assignee's own model or the default."""
    if id_ is None:
        n = int(plan.get("next_id") or 1)
        plan["next_id"] = n + 1
        id_ = f"i{n}"
    return {"id": id_, "title": " ".join((title or "").split())[:120] or f"item {id_}",
            "brief": (brief or "").strip(), "depends_on": list(depends_on),
            "status": "todo", "assignee": (assignee or "").strip() or None,
            "model": (str(model).strip() or None) if model else None,
            "attempts": 0, "stalls": 0, "last_error": None, "result_summary": None,
            "conversation_id": None, "notes": [], "report": None}


def normalise(plan: dict) -> dict:
    """Fill defaults, keep ids unique, drop dangling/self dependencies and break
    cycles — so nothing downstream has to defend against a hand-edited file."""
    base = empty_plan()
    for k, v in base.items():
        plan.setdefault(k, v)
    plan["attempts_max"] = max(1, min(int(plan.get("attempts_max") or 1), MAX_ATTEMPTS_CAP))
    plan["max_concurrent"] = max(1, min(int(plan.get("max_concurrent") or 1),
                                        MAX_CONCURRENT_CAP))
    plan["max_iterations"] = max(0, int(plan.get("max_iterations") or 0))
    if plan.get("status") not in ("draft", "running", "done", "failed", "stopped"):
        plan["status"] = "draft"
    raws = [r for r in list(plan.get("items") or [])[:MAX_ITEMS] if isinstance(r, dict)]
    # ids like i7 must never be reissued: the counter sits above every explicit one
    explicit = [str(r.get("id") or "").strip() for r in raws]
    nums = [int(i[1:]) for i in explicit if re.fullmatch(r"i\d+", i)]
    plan["next_id"] = max([int(plan.get("next_id") or 1), *[n + 1 for n in nums]])
    items, seen = [], set()
    for raw, rid in zip(raws, explicit):
        it = new_item(plan, title=str(raw.get("title") or ""),
                      brief=str(raw.get("brief") or ""),
                      depends_on=[str(d) for d in (raw.get("depends_on") or [])],
                      assignee=raw.get("assignee"),
                      id_=rid if rid and rid not in seen else None,
                      model=raw.get("model"))
        seen.add(it["id"])
        it["status"] = raw.get("status") if raw.get("status") in STATUSES else "todo"
        it["attempts"] = max(0, int(raw.get("attempts") or 0))
        it["stalls"] = max(0, int(raw.get("stalls") or 0))
        it["last_error"] = raw.get("last_error") or None
        it["result_summary"] = raw.get("result_summary") or None
        it["conversation_id"] = raw.get("conversation_id") or None
        it["notes"] = [n for n in (raw.get("notes") or []) if isinstance(n, dict)]
        it["report"] = raw.get("report") if isinstance(raw.get("report"), dict) else None
        items.append(it)
    ids = {i["id"] for i in items}
    for it in items:
        it["depends_on"] = [d for d in dict.fromkeys(it["depends_on"])
                            if d in ids and d != it["id"]]
    _break_cycles(items)
    plan["items"] = items
    release_blocked(plan)
    return plan


def _break_cycles(items: list[dict]) -> None:
    """Drop exactly the back-edges: a depth-first walk in checklist order, and
    a dependency on an item still on the walk's stack is a cycle, so that edge
    goes. Everything downstream of a cycle keeps its edges — a planner slip
    between two items must not detach a third."""
    deps = {it["id"]: it["depends_on"] for it in items}
    state: dict[str, int] = {}                 # 1 = on the stack, 2 = finished

    def visit(v: str) -> None:
        state[v] = 1
        keep = []
        for d in deps[v]:
            st = state.get(d)
            if st == 1:
                continue                       # back-edge
            if st is None:
                visit(d)
            keep.append(d)
        deps[v][:] = keep
        state[v] = 2

    for it in items:
        if state.get(it["id"]) is None:
            visit(it["id"])


def load(slug: str) -> dict | None:
    p = writes.resolve(slug, PLAN_FILE)
    if p is None:
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    return normalise(data) if isinstance(data, dict) else None


async def save(slug: str, plan: dict) -> None:
    plan["updated_at"] = _now()
    await writes.apply_write(slug, PLAN_FILE,
                             (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode())


def _lock(slug: str) -> asyncio.Lock:
    return _locks.setdefault(slug, asyncio.Lock())


@contextlib.asynccontextmanager
async def edit(slug: str):
    """Read-modify-write under the project's lock. Raises LookupError when the
    project has no plan: an edit needs something to edit."""
    async with _lock(slug):
        plan = load(slug)
        if plan is None:
            raise LookupError(f"project {slug!r} has no plan")
        yield plan
        await save(slug, plan)


def is_running(slug: str) -> bool:
    t = _runs.get(slug)
    return t is not None and not t.done()


def public(plan: dict | None, slug: str | None = None) -> dict | None:
    """What the API returns: the file plus the live flag the file cannot know."""
    if plan is None:
        return None
    return {**plan, "running": is_running(slug) if slug else False}


# --- dependency resolution (pure) --------------------------------------------

def index(plan: dict) -> dict[str, dict]:
    return {it["id"]: it for it in plan["items"]}


def ready(plan: dict) -> list[dict]:
    """todo items whose every dependency is settled, in checklist order."""
    idx = index(plan)
    out = []
    for it in plan["items"]:
        if it["status"] != "todo":
            continue
        deps = [idx.get(d) for d in it["depends_on"]]
        if all(d is not None and d["status"] in SETTLED for d in deps):
            out.append(it)
    return out


def propagate_blocked(plan: dict) -> list[dict]:
    """todo items behind a failed/blocked (or vanished) dependency become
    blocked; blocking cascades. Returns the items this call changed."""
    idx = index(plan)
    changed: list[dict] = []
    moved = True
    while moved:
        moved = False
        for it in plan["items"]:
            if it["status"] != "todo":
                continue
            for d in it["depends_on"]:
                dep = idx.get(d)
                if dep is None or dep["status"] in ("failed", "blocked"):
                    it["status"] = "blocked"
                    it["last_error"] = (f"{DEP_BLOCK}{d} is missing" if dep is None
                                        else f"{DEP_BLOCK}{d} {dep['status']}")
                    changed.append(it)
                    moved = True
                    break
    return changed


def release_blocked(plan: dict) -> list[dict]:
    """The inverse of propagate_blocked: an item the RUNNER blocked because a
    dependency failed goes back to todo once no dependency is failed/blocked
    any more — the operator reset or deleted the failure — and the release
    cascades down the chain. An item that blocked ITSELF (reported "blocked":
    it needs the operator) is left alone. Pure; returns what it changed."""
    idx = index(plan)
    changed: list[dict] = []
    moved = True
    while moved:
        moved = False
        for it in plan["items"]:
            if it["status"] != "blocked" or not (it.get("last_error") or "").startswith(DEP_BLOCK):
                continue
            deps = [idx.get(d) for d in it["depends_on"]]
            if all(d is not None and d["status"] not in ("failed", "blocked") for d in deps):
                it["status"], it["last_error"] = "todo", None
                changed.append(it)
                moved = True
    return changed


def finished(plan: dict) -> bool:
    return not ready(plan) and not any(it["status"] == "running" for it in plan["items"])


# --- the planner pass --------------------------------------------------------

PLANNER_SYSTEM = """You are a planning assistant for a team of AI agents working
on one software/knowledge project. Turn the operator's dump into an ordered
checklist of work items, each small enough for one agent in one sitting and
self-contained enough that an agent who has NOT read the dump can do it from
the brief alone.

Reply with ONLY a JSON array, no prose, no markdown fence:
[{"title": "<= 80 chars", "brief": "concrete, self-contained instructions: what to do, where (paths), what done looks like", "depends_on": [<0-based indices of EARLIER items whose results this one needs>], "assignee": "<agent slug from the roster, or null>"}]

Rules: 3 to 12 items. Items with no dependency run in PARALLEL, so keep
independent work independent and put shared groundwork first. Prefer fewer,
larger items over many tiny ones. Never invent an assignee that is not in the
roster; null means a general worker."""

# appended to the planner's instructions only when the operator made explicit
# model assignments (orchestrate's `models`)
PLANNER_MODELS = """
Model assignments: the operator said which model to use for some of the work
(listed under "# Model assignments"). Add a "model" key to EVERY item: the
assigned model id, exactly as listed, on each item that does that work, and
null on every other item. Never put a model on an item the operator did not
assign one to."""

SYNTH_SYSTEM = """Write the closing report for a multi-agent plan run: what got
done (with the exact paths/artifacts the items reported), what failed or was
blocked and why, and what the operator should do next. Tight markdown, no
preamble, no restating the checklist verbatim."""


def _read_refs(slug: str, files) -> str:
    parts = []
    for rel in list(files or [])[:8]:
        try:
            p = writes.resolve(slug, str(rel))
        except Exception:  # noqa: BLE001 — a bad path is skipped, not fatal
            p = None
        if p is None:
            parts.append(f"## {rel}\n(no such file)")
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        parts.append(f"## {rel}\n{text[:PLANNER_FILE_CHARS]}")
    return ("# Referenced files\n" + "\n\n".join(parts)) if parts else ""


def _parse_items(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for raw in data if isinstance(data, list) else []:
        if isinstance(raw, dict) and (raw.get("title") or raw.get("brief")):
            out.append(raw)
    return out


def _title_from(dump: str) -> str:
    first = next((ln.strip(" #-*") for ln in dump.splitlines() if ln.strip()), "")
    return " ".join(first.split())[:60] or "Plan"


def _known_agents() -> set[str]:
    d = settings.agents_dir
    return {p.name for p in d.iterdir() if (p / "AGENT.md").is_file()} if d.is_dir() else set()


def checked_models(models) -> list[dict]:
    """The operator's explicit model assignments, each model validated against
    the enabled list. [{"task": what the operator called that work, "model":
    canonical provider/model}]. ValueError names the first bad one — a model
    the operator asked for that cannot run is something to tell them, not to
    quietly replace with the default."""
    from . import providers
    out = []
    for m in models or []:
        if not isinstance(m, dict):
            raise ValueError("each model assignment is {task, model}")
        task = " ".join(str(m.get("task") or "").split())[:200]
        name = str(m.get("model") or "").strip()
        if not task or not name:
            raise ValueError("each model assignment needs both a task and a model")
        try:
            out.append({"task": task, "model": providers.checked(name)})
        except providers.ProviderError as e:
            raise ValueError(str(e)) from None
    return out


def _assigned_model(raw: dict, allowed: set[str]) -> str | None:
    """The planner's model for an item, only if it is one the operator
    assigned. The planner is a model and can slip; a model the operator never
    named must not reach an item through it."""
    from . import providers
    name = str(raw.get("model") or "").strip()
    if not name or not allowed:
        return None
    try:
        full = providers.canonical(name)
    except providers.ProviderError:
        return None
    return full if full in allowed else None


async def plan_from_dump(slug: str, dump: str, files=(), *, title: str = "",
                         models=()) -> dict:
    """One model call turns the dump into the checklist and persists it as the
    project's plan. Refuses to replace a plan that is running.

    `models` is the operator's explicit model assignments (checked_models'
    shape, already validated); without it every item runs on its assignee's
    model or the default."""
    dump = (dump or "").strip()
    if not dump:
        raise ValueError("the dump is empty — give the planner something to plan")
    if is_running(slug):
        raise RuntimeError("a plan run is in progress — stop it before re-planning")
    roster = agents_index() or "(no named agents — leave assignee null)"
    user = (f"Project: {slug}\n\n# Dump\n{dump[:PLANNER_DUMP_CHARS]}\n\n"
            f"{_read_refs(slug, files)}\n\n# Roster\n{roster}")
    system = PLANNER_SYSTEM
    if models:
        system += PLANNER_MODELS
        user += "\n\n# Model assignments\n" + "\n".join(
            f"- {m['task']}: {m['model']}" for m in models)
    allowed = {m["model"] for m in models or ()}
    text = await complete_text(system, user)
    raws = _parse_items(text)
    if not raws:
        raise ValueError("the planner returned no checklist items")
    known = _known_agents()
    plan = empty_plan(title=title or _title_from(dump), dump=dump)
    items = []
    for raw in raws[:MAX_ITEMS]:
        assignee = str(raw.get("assignee") or "").strip() or None
        items.append(new_item(plan, title=str(raw.get("title") or raw.get("brief"))[:120],
                              brief=str(raw.get("brief") or raw.get("title")),
                              assignee=assignee if assignee in known else None,
                              model=_assigned_model(raw, allowed)))
    # depends_on arrives as indices, ids or titles — resolve all three
    by_title = {it["title"].lower(): it["id"] for it in items}
    for it, raw in zip(items, raws):
        deps = []
        for d in raw.get("depends_on") or []:
            if isinstance(d, int) and 0 <= d < len(items):
                deps.append(items[d]["id"])
            elif isinstance(d, str):
                ds = d.strip()
                if ds.isdigit() and int(ds) < len(items):
                    deps.append(items[int(ds)]["id"])
                elif ds in {i["id"] for i in items}:
                    deps.append(ds)
                elif ds.lower() in by_title:
                    deps.append(by_title[ds.lower()])
        it["depends_on"] = deps
    plan["items"] = items
    plan = normalise(plan)
    async with _lock(slug):
        await save(slug, plan)
    return plan


# --- operator edits ----------------------------------------------------------

def apply_item_edit(plan: dict, it: dict, patch: dict) -> None:
    """The operator-editable fields. Runner-owned fields (attempts, errors,
    results, conversation) are never taken from a request."""
    if "title" in patch and patch["title"] is not None:
        it["title"] = " ".join(str(patch["title"]).split())[:120] or it["title"]
    if "brief" in patch and patch["brief"] is not None:
        it["brief"] = str(patch["brief"]).strip()
    if "depends_on" in patch and patch["depends_on"] is not None:
        it["depends_on"] = [str(d) for d in patch["depends_on"]]
    if "assignee" in patch:
        a = (patch["assignee"] or "").strip() or None
        if a and a not in _known_agents():
            raise ValueError(f"no agent named {a!r}")
        it["assignee"] = a
    if "model" in patch:
        # the operator's own edit is an explicit choice by definition; it still
        # has to be a model that can run
        m = (patch["model"] or "").strip() or None
        if m:
            from . import providers
            try:
                m = providers.checked(m)
            except providers.ProviderError as e:
                raise ValueError(str(e)) from None
        it["model"] = m
    if "status" in patch and patch["status"] is not None:
        st = patch["status"]
        if st not in OPERATOR_STATUSES:
            raise ValueError(f"status must be one of {', '.join(OPERATOR_STATUSES)}")
        it["status"] = st
        if st == "todo":
            # a manual reset is a fresh start: the old report must not settle it
            it["report"] = None
            it["last_error"] = None
    if "position" in patch and patch["position"] is not None:
        items = plan["items"]
        items.remove(it)
        pos = max(0, min(int(patch["position"]), len(items)))
        items.insert(pos, it)


def replace_items(plan: dict, incoming: list[dict]) -> None:
    """PUT semantics: the request's order and operator fields win; an item
    whose id already exists keeps its runner-owned fields; a running item
    keeps running unless the request says otherwise."""
    old = index(plan)
    items = []
    for raw in incoming[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("id") or "").strip()
        it = old.get(rid) if rid else None
        if it is None:
            fresh = bool(rid) and rid not in {i["id"] for i in items}
            it = new_item(plan, title=str(raw.get("title") or ""), id_=rid if fresh else None)
        patch = {k: raw[k] for k in ("title", "brief", "depends_on", "assignee", "model")
                 if k in raw}
        if raw.get("status") in OPERATOR_STATUSES and raw.get("status") != it["status"]:
            patch["status"] = raw["status"]
        apply_item_edit(plan, it, patch)
        items.append(it)
    plan["items"] = items


# --- the run -----------------------------------------------------------------

ITEM_PROMPT = """You are one worker on a team of agents executing an explicit
plan for this project. You get exactly one checklist item. Do it completely,
stay inside it, coordinate with teammates by message when your work touches
theirs, and report the outcome with plan_report before you stop."""

REPORT_RULES = """# Reporting — required
When the item is finished, call plan_report with status "done" and a summary
(what you did, the exact file paths, what the items after you need to know).
If you cannot finish: plan_report with status "failed" (something went wrong
that a retry could fix — say what) or "blocked" (needs the operator — say what).
A final reply without a plan_report call counts as a failed attempt.

# Teammates
Other items of this plan run in parallel as separate agents. send_message with
to="?" lists them; address one as item:<id> (for example to="item:i2"). Say so
before you touch files another item owns; ask when only a teammate knows the
answer; a message to an item that has not started yet is kept and shown to it
when it starts. Messages to you arrive between your reasoning rounds."""


def _item_task(plan: dict, it: dict, deps: list[dict]) -> str:
    parts = [f"[item {it['id']}] {it['title']}",
             f"\nYou are working item {it['id']} of the plan \"{plan['title']}\". "
             "Do exactly this item, nothing more.",
             f"\n# Brief\n{it['brief'] or it['title']}"]
    if deps:
        parts.append("\n# Results from the items this depends on")
        for d in deps:
            parts.append(f"## {d['id']} — {d['title']} ({d['status']})\n"
                         f"{d.get('result_summary') or '(no summary was reported)'}")
    if it.get("notes"):
        parts.append("\n# Notes teammates left for you")
        parts += [f"- from {n.get('from', 'a teammate')}: {n.get('body', '')}"
                  for n in it["notes"]]
    if it.get("attempts", 0) > 1 and it.get("last_error"):
        parts.append(f"\n# Previous attempt\nAttempt {it['attempts'] - 1} ended with: "
                     f"{it['last_error']}\nFix the cause; do not repeat it.")
    parts.append("\n" + REPORT_RULES)
    return "\n".join(parts)


def _item_agent(plan: dict, it: dict) -> dict:
    """The definition an item runs as: the named assignee's AGENT.md, or a lean
    temporary worker. Either way the plan's iteration cap, if set, applies."""
    from . import agents_run
    if it.get("assignee"):
        from .agents_api import _read
        agent = dict(_read(it["assignee"]))         # HTTPException 404 if gone
    else:
        agent = {"name": f"item {it['id']}", "prompt": ITEM_PROMPT, "description": "",
                 "model": "", "base_url": "", "own_memory": False,
                 "context_exclude": list(agents_run.TEMP_LEAN_EXCLUDE),
                 "tools_exclude": [], "skills_exclude": [], "max_iterations": 0,
                 "project": ""}
    if plan.get("max_iterations"):
        agent["max_iterations"] = plan["max_iterations"]
    if it.get("model"):
        # re-checked at spawn: a model switched off since planning fails the
        # attempt out loud (the error lands on the item) instead of the item
        # silently running on something the operator did not choose
        from . import providers
        agent["model"], agent["base_url"] = providers.checked(it["model"]), ""
    return agent


def _public_item(it: dict) -> dict:
    return {k: v for k, v in it.items() if k != "report"}


def _emit_item(job_id: str, it: dict) -> None:
    bus.publish(job_id, {"type": "plan_item", "job_id": job_id, **_public_item(it)})


def live_item(cid: int | None) -> dict | None:
    """agentmsg's view: is this live conversation a plan item, and which."""
    return _live_items.get(cid) if cid is not None else None


def resolve_item(project: str | None, item_id: str) -> int | None:
    """The running conversation for `item:<id>`, preferring the sender's
    project when two plans happen to share an id."""
    hits = [(cid, v) for cid, v in _live_items.items() if v["item_id"] == item_id]
    for cid, v in hits:
        if v["project"] == project:
            return cid
    return hits[0][0] if hits else None


async def leave_note(project: str | None, item_id: str, *, sender: str,
                     body: str) -> str | None:
    """A message to an item that is not running: kept on the item and shown in
    its brief when it starts. Returns None when queued, else why not."""
    if not project:
        return "you are not in a project, so there is no plan to leave a note in"
    try:
        async with edit(project) as plan:
            it = index(plan).get(item_id)
            if it is None:
                return f"the plan for project {project} has no item {item_id!r}"
            if it["status"] != "todo":
                return (f"item {item_id} is {it['status']}, not running — it will not "
                        "read a message now; its outcome is in the plan")
            it["notes"].append({"from": sender, "body": body[:SUMMARY_CHARS], "at": _now()})
    except LookupError:
        return f"project {project} has no plan"
    return None


async def report(slug: str, *, cid: int | None, item_id: str | None,
                 status: str, summary: str) -> str:
    """The plan_report tool. The item is the one whose running conversation is
    the caller's — an item can only ever report itself. `item_id`, if given,
    must agree."""
    if status not in REPORT_STATUSES:
        return f"error: status must be one of {', '.join(REPORT_STATUSES)}"
    if not cid:
        return "error: plan_report only works inside a running plan item"
    try:
        async with edit(slug) as plan:
            it = next((i for i in plan["items"] if i.get("conversation_id") == cid), None)
            if it is None:
                return "error: this turn is not a plan item, so there is nothing to report on"
            if item_id and item_id != it["id"]:
                return (f"error: this turn is item {it['id']}, not {item_id} — an item "
                        "reports only itself")
            it["report"] = {"status": status, "summary": (summary or "").strip()[:SUMMARY_CHARS],
                            "at": _now()}
            it["result_summary"] = it["report"]["summary"] or None
            job_id = plan.get("job_id")
    except LookupError:
        return "error: this project has no plan"
    if job_id:
        _emit_item(job_id, it)
    return (f"recorded: item {it['id']} -> {status}. Finish your reply now; no further "
            "tool calls are needed.")


def _fenced_json(text: str) -> dict | None:
    """The fallback completion signal: the last ```json block in the final reply."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("status") in REPORT_STATUSES:
            return data
    return None


async def _open_head(slug: str, plan: dict, job_id: str) -> tuple[int, str | None]:
    db = await get_db()
    try:
        launched_by, owner = await launcher(db)
        root_id = await open_conversation(
            db, project=slug, title=f"[head] Plan: {plan['title'][:50]}",
            kind="head", job_id=job_id, parent=launched_by)
    finally:
        await db.close()
    return root_id, owner


async def start_run(slug: str, *, peak: bool = False) -> dict:
    """Launch the runner as a detached task. Returns {job_id, root_id}. Raises
    RuntimeError when a run is already live or there is nothing to run."""
    if is_running(slug):
        raise RuntimeError("a plan run is already in progress")
    plan = load(slug)
    if plan is None:
        raise RuntimeError("this project has no plan yet")
    # "running" with no live task is a run the process lost (a restart): _drive
    # puts those back to todo, so they count as work here
    if not any(it["status"] in ("todo", "running") for it in plan["items"]):
        raise RuntimeError("nothing to run — every item is done, skipped, failed or blocked")
    job_id = uuid.uuid4().hex
    root_id, owner = await _open_head(slug, plan, job_id)
    task = asyncio.create_task(run_plan(slug, job_id=job_id, root_id=root_id,
                                        peak=peak, owner=owner))
    _runs[slug] = task

    def _done(t: asyncio.Task) -> None:
        if _runs.get(slug) is t:
            _runs.pop(slug, None)
        if not t.cancelled():
            t.exception()                    # retrieved: never an "unretrieved" warning
    task.add_done_callback(_done)
    return {"job_id": job_id, "root_id": root_id}


def stop_run(slug: str) -> bool:
    t = _runs.get(slug)
    if t is None or t.done():
        return False
    t.cancel()
    return True


async def run_plan(slug: str, *, job_id: str, root_id: int, peak: bool = False,
                   owner: str | None = None) -> dict:
    """The whole run, start to rollup. Owns its Budget (a plan run is an
    operation of its own even when a chat's tool started it), pins the project
    for every item, and finishes with a rollup whatever happened."""
    plan = load(slug)
    title = plan["title"] if plan else "Plan"
    budget = budget_mod.Budget(settings.max_op_input_tokens, settings.max_op_output_tokens)
    budget_mod.register(job_id, budget)
    optok = budget_mod.active_op_id.set(job_id)
    ptoken = runtime.active_project.set(slug)
    cidtoken = runtime.conversation_id.set(root_id)     # items' parent (db.launcher)
    wtoken = runtime.web_session.set(f"job:{job_id}")
    # items sit one hop under the plan: they may spawn helpers, helpers may not
    dtoken = runtime.spawn_depth.set(1)
    if peak:
        confirm_peak(root_id)
    bus.publish(job_id, {"type": "job_start", "job_id": job_id, "root_id": root_id,
                         "agent_slug": owner})
    bus.publish(job_id, {"type": "node_spawned", "node_id": root_id, "parent_id": None,
                         "kind": "head", "title": f"Plan: {title}", "depth": 0,
                         "agent_slug": owner})
    bus.announce_job(job_id, root_id, f"Plan: {title}")
    status, rollup = "failed", ""
    try:
        async with orchestrator.job_workspace(slug, top_level=True):
            status = await _drive(slug, job_id, root_id)
        bus.publish(job_id, {"type": "node_status", "node_id": root_id,
                             "status": "summarizing"})
        rollup = await _synthesize(slug, status)
    except asyncio.CancelledError:
        status = "stopped"
        rollup = await _synthesize(slug, status)
    except Exception as e:  # noqa: BLE001 — the run must end with a rollup
        status = "failed"
        rollup = f"error: {e}\n\n" + await _synthesize(slug, status)
        bus.publish(job_id, {"type": "error", "node_id": root_id, "message": str(e)})
    finally:
        try:
            async with _lock(slug):
                p = load(slug)
                if p is not None:
                    p["status"] = status
                    for it in p["items"]:
                        if it["status"] == "running":     # stopped mid-flight
                            it["status"] = "todo"
                            _live_items.pop(it.get("conversation_id"), None)
                    await save(slug, p)
            db = await get_db()
            try:
                await db.execute("UPDATE conversations SET rollup = ? WHERE id = ?",
                                 (rollup, root_id))
                await db.commit()
            finally:
                await db.close()
            await writes.apply_write(slug, f"runs/{job_id}/{root_id}-head.md", rollup.encode())
        except Exception:  # noqa: BLE001 — persistence is best-effort at teardown
            pass
        bus.publish(job_id, {"type": "node_done", "node_id": root_id, "rollup": rollup})
        bus.publish(job_id, {"type": "job_final", "job_id": job_id, "root_id": root_id,
                             "rollup": rollup, "usage": budget.summary(),
                             "plan_status": status})
        bus.close_job(job_id)
        runtime.spawn_depth.reset(dtoken)
        runtime.web_session.reset(wtoken)
        runtime.conversation_id.reset(cidtoken)
        runtime.active_project.reset(ptoken)
        budget_mod.active_op_id.reset(optok)
        budget_mod.release(job_id)
    return {"root_id": root_id, "job_id": job_id, "status": status, "rollup": rollup,
            "usage": budget.summary()}


async def _drive(slug: str, job_id: str, root_id: int) -> str:
    """The monitoring loop: spawn what is ready, settle what finished, block
    what cannot run, nudge and re-drive what stalled, honour operator edits.
    Returns the run's final status."""
    tasks: dict[str, asyncio.Task] = {}
    meta: dict[str, dict] = {}
    spawned = 0
    async with edit(slug) as plan:
        plan["status"], plan["job_id"], plan["root_id"] = "running", job_id, root_id
        for it in plan["items"]:
            if it["status"] == "running":          # a previous run's leftovers
                it["status"] = "todo"
    try:
        while True:
            async with edit(slug) as plan:
                idx = index(plan)
                # operator edits: an item that is no longer running in the file
                # (marked done, reset, skipped, deleted) loses its task
                for iid, t in list(tasks.items()):
                    it = idx.get(iid)
                    if it is None or it["status"] != "running":
                        t.cancel()
                        tasks.pop(iid)
                        _live_items.pop(meta.pop(iid, {}).get("cid"), None)
                # completions
                for iid, t in list(tasks.items()):
                    if t.done():
                        tasks.pop(iid)
                        await _settle(plan, idx[iid], t, meta.pop(iid), job_id)
                # after settling, so a dependency that just failed for the last
                # time blocks its dependants in the same tick the run may end on
                for it in propagate_blocked(plan):
                    _emit_item(job_id, it)
                # stalls: quiet past the window -> one nudge; still quiet -> cancel
                # and re-spawn once, then it is a failure
                now = time.monotonic()
                for iid, t in list(tasks.items()):
                    m, it = meta[iid], idx[iid]
                    if now - m["last_activity"] < settings.plan_stall_seconds:
                        continue
                    if not m["nudged"]:
                        m["nudged"] = True
                        m["last_activity"] = now
                        await _nudge(root_id, it, m)
                        continue
                    t.cancel()
                    tasks.pop(iid)
                    meta.pop(iid)
                    _live_items.pop(m.get("cid"), None)
                    it["stalls"] += 1
                    it["last_error"] = "stalled: no tool call or message for too long"
                    it["status"] = "todo" if it["stalls"] <= 1 else "failed"
                    _emit_item(job_id, it)
                # spawn what is ready, within the concurrency and spawn caps
                while len(tasks) < plan["max_concurrent"] and spawned < MAX_SPAWNS:
                    nxt = next((it for it in ready(plan) if it["id"] not in tasks), None)
                    if nxt is None:
                        break
                    nxt["status"] = "running"
                    nxt["attempts"] += 1
                    nxt["report"] = None
                    deps = [idx[d] for d in nxt["depends_on"] if d in idx]
                    m = {"cid": None, "last_activity": time.monotonic(),
                         "nudged": False}
                    meta[nxt["id"]] = m
                    tasks[nxt["id"]] = asyncio.create_task(
                        _run_item(slug, job_id, root_id, plan, nxt, deps, m))
                    spawned += 1
                    _emit_item(job_id, nxt)
                if not tasks and (finished(plan) or spawned >= MAX_SPAWNS):
                    if spawned >= MAX_SPAWNS and ready(plan):
                        for it in ready(plan):
                            it["status"] = "failed"
                            it["last_error"] = f"run hit the spawn cap ({MAX_SPAWNS})"
                            _emit_item(job_id, it)
                    return ("done" if all(it["status"] in SETTLED for it in plan["items"])
                            else "failed")
            if tasks:
                await asyncio.wait(list(tasks.values()), timeout=settings.plan_tick_seconds,
                                   return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(settings.plan_tick_seconds)
    finally:
        for t in tasks.values():
            t.cancel()
        for m in meta.values():
            _live_items.pop(m.get("cid"), None)
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)


async def _run_item(slug: str, job_id: str, root_id: int, plan: dict, it: dict,
                    deps: list[dict], m: dict) -> dict:
    """One attempt of one item, as a headless agent run under the plan's head."""
    from . import agents_run
    task = _item_task(plan, it, deps)
    item_id, title = it["id"], it["title"]

    async def on_open(cid: int) -> None:
        m["cid"] = cid
        _live_items[cid] = {"project": slug, "item_id": item_id, "title": title}
        async with edit(slug) as p:
            cur = index(p).get(item_id)
            if cur is not None:
                cur["conversation_id"] = cid
                _emit_item(job_id, cur)
        bus.publish(job_id, {"type": "node_spawned", "node_id": cid, "parent_id": root_id,
                             "kind": "agent", "title": f"{item_id} · {title}", "depth": 1,
                             "agent_slug": it.get("assignee"), "item_id": item_id})
        bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "running"})

    def on_event(ev: dict) -> None:
        m["last_activity"] = time.monotonic()
        if ev.get("type") == "tool" and m["cid"] is not None:
            bus.publish(job_id, {"type": "tool", "name": ev.get("name"), "node_id": m["cid"]})

    try:
        return await agents_run._run_headless(
            _item_agent(plan, it), task, active=slug, job_id=job_id,
            title=f"[item {item_id}] {title[:50]}", on_open=on_open, on_event=on_event,
            extra_tools=("plan_report",))
    finally:
        _live_items.pop(m.get("cid"), None)


async def _settle(plan: dict, it: dict, t: asyncio.Task, m: dict, job_id: str) -> None:
    """Check a finished attempt off — or schedule its retry."""
    cid = m.get("cid")
    if t.cancelled():
        return                              # whoever cancelled it already set the status
    exc = t.exception()
    if isinstance(exc, BudgetExceeded):
        raise exc                           # the run is over, not just this item
    rep = it.get("report") or {}
    final = ""
    if exc is None:
        final = (t.result() or {}).get("final") or ""
    if exc is not None:
        status, err, summary = "failed", f"{type(exc).__name__}: {exc}", None
    elif rep.get("status"):
        status, err, summary = rep["status"], None, rep.get("summary")
    else:
        block = _fenced_json(final)
        if block:
            status, err, summary = block["status"], None, str(block.get("summary") or "")
        else:
            status, err, summary = "failed", "no structured completion report (plan_report was not called)", None
    if status in ("failed", "blocked"):
        err = err or summary or "the agent reported no reason"
    if status == "failed" and it["attempts"] < plan["attempts_max"]:
        it["status"] = "todo"               # retried on the next tick
    else:
        it["status"] = status
    it["last_error"] = err
    it["result_summary"] = (summary or "").strip()[:SUMMARY_CHARS] or (
        " ".join(final.split())[:SUMMARY_CHARS] or None)
    _emit_item(job_id, it)
    if cid is not None:
        if status == "done":
            bus.publish(job_id, {"type": "node_done", "node_id": cid,
                                 "rollup": it["result_summary"] or ""})
        else:
            bus.publish(job_id, {"type": "error", "node_id": cid, "message": err or status})


async def _nudge(root_id: int, it: dict, m: dict) -> None:
    """A stalled item gets a message from the plan's head, as a teammate would
    send it — through the same inbox its siblings use."""
    if m.get("cid") is None:
        return
    from . import agentmsg
    db = await get_db()
    try:
        await agentmsg.send(
            db, sender_cid=root_id, to=str(m["cid"]),
            body=(f"[plan] item {it['id']} has been quiet for a while. If you are stuck, "
                  "call plan_report with status \"blocked\" and say what you need; "
                  "otherwise keep going and call plan_report when the item is done."))
    except Exception:  # noqa: BLE001 — a failed nudge is not a failed item
        pass
    finally:
        await db.close()


def _listing(plan: dict) -> str:
    return "\n".join(
        f"- [{it['status']}] {it['id']} {it['title']}"
        + (f": {it['result_summary']}" if it.get("result_summary") else "")
        + (f" (error: {it['last_error']})" if it.get("last_error") and it["status"] != "done" else "")
        for it in plan["items"])


async def _synthesize(slug: str, status: str) -> str:
    """The head's rollup: a model-written closing report when the model is
    reachable, the plain listing when it is not — a run always ends with one."""
    plan = load(slug)
    if plan is None:
        return f"Plan run {status}."
    listing = _listing(plan)
    if status != "stopped":
        try:
            text = await complete_text(
                SYNTH_SYSTEM, f"Plan: {plan['title']}\nRun status: {status}\n\n{listing}")
            if text:
                return text
        except Exception:  # noqa: BLE001 — fall through to the listing
            pass
    return f"Plan run {status}.\n\n{listing}"


def render_checklist(plan: dict) -> str:
    """The checklist as the model or a terminal reads it."""
    lines = []
    for it in plan["items"]:
        deps = f" (after {', '.join(it['depends_on'])})" if it["depends_on"] else ""
        who = f" @{it['assignee']}" if it.get("assignee") else ""
        mdl = f" (model {it['model']})" if it.get("model") else ""
        lines.append(f"- {it['id']} [{it['status']}]{who} {it['title']}{deps}{mdl}")
    return "\n".join(lines)


# --- orchestrator mode (POST /api/chat mode=orchestrate) -----------------------

ORCHESTRATOR_PROMPT = """# You are this conversation's orchestrator
The operator hands you a brain-dump; you get it done through a team of agents
working in project {project}, and you stay in charge until it is finished. Do
not do the items' work yourself.

1. Plan and launch: call orchestrate with the operator's dump — verbatim, plus
   any facts from this conversation the agents need (they will not see it). It
   saves an explicit checklist and runs one agent per item in this project, on
   this host, under this project's egress policy, dependencies respected.
2. Monitor: call plan_status with wait_seconds (e.g. 300). It returns when an
   item changes state, a message arrives for you, or the wait runs out, and
   shows every item's status, its agent's conversation id, and its result or
   error. Keep calling it until the run is finished.
3. Steer: send_message to item:<id> (or the item's conversation id) to correct,
   unblock or inform a running agent. A failed or blocked item: message it,
   or tell the operator what it needs.
4. A single focused task outside the plan can go to spawn_agent or
   spawn_temp_agent; you wait for that one's report.
5. When plan_status says the run is finished, report to the operator: what got
   done (exact paths), what failed or is blocked and why, and what they need
   to decide.

Model choice: every agent runs on the default model. ONLY when the operator
explicitly said which model to use for which task, pass that: orchestrate's
`models` ({{task, model}} per assignment) or spawn_agent / spawn_temp_agent's
`model`. Never choose a model on your own initiative.

Messages from the operator can arrive while you work. They are the operator
speaking: act on them (message the affected agents, adjust the plan)."""


def orchestrator_prompt(project: str | None) -> str:
    return ORCHESTRATOR_PROMPT.format(
        project=project or "(none — this conversation lost its project; tell the operator)")


# how often plan_status re-reads the plan while waiting; a module constant so a
# test can shrink it
STATUS_POLL_SECONDS = 2.0


def _fingerprint(plan: dict | None, running: bool) -> tuple:
    if plan is None:
        return (None,)
    return (plan.get("status"), running, plan.get("job_id"),
            tuple((it["id"], it["status"], it.get("attempts"),
                   (it.get("report") or {}).get("status"), it.get("conversation_id"))
                  for it in plan["items"]))


async def _pending_for(cid: int | None) -> int:
    """Undelivered messages waiting for conversation `cid` — the operator's or
    an agent's. A waiting plan_status returns early for them: the orchestrator
    reads its inbox only between rounds, so sitting out the wait would sit on
    the message too."""
    if not cid:
        return 0
    db = await get_db()
    try:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM agent_messages WHERE delivered_at IS NULL "
            "AND to_conversation_id = ?", (cid,)) as cur:
            return (await cur.fetchone())["n"]
    finally:
        await db.close()


async def _head_rollup(root_id) -> str | None:
    if not root_id:
        return None
    db = await get_db()
    try:
        async with db.execute("SELECT rollup FROM conversations WHERE id = ?",
                              (root_id,)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    return row["rollup"] if row else None


def _status_text(plan: dict, running: bool, rollup: str | None, why: str) -> str:
    lines = [f"Plan '{plan['title']}' — {'RUNNING' if running else 'not running'} "
             f"(status {plan['status']}, head conversation {plan.get('root_id')}). {why}"]
    for it in plan["items"]:
        who = f" @{it['assignee']}" if it.get("assignee") else ""
        mdl = f" model {it['model']}" if it.get("model") else ""
        cid = f" conv {it['conversation_id']}" if it.get("conversation_id") else ""
        lines.append(f"- {it['id']} [{it['status']}]{who}{mdl}{cid} {it['title']}")
        if it.get("result_summary"):
            lines.append(f"    result: {it['result_summary']}")
        if it.get("last_error") and it["status"] != "done":
            lines.append(f"    error: {it['last_error']}")
    if not running and rollup:
        lines.append(f"\n# Closing rollup\n{rollup}")
    elif running:
        lines.append("\nRunning items can be messaged: send_message to item:<id>.")
    return "\n".join(lines)


async def status(slug: str, *, wait_seconds: int = 0, cid: int | None = None) -> str:
    """The plan_status tool: the checklist as it stands, after waiting (up to
    wait_seconds, capped) for it to change or for a message to arrive for the
    caller. Waiting on a running plan is how an orchestrator supervises one
    without burning a model call every few seconds."""
    wait = max(0, min(int(wait_seconds or 0), settings.plan_status_max_wait))
    plan, running = load(slug), is_running(slug)
    if plan is None:
        return "error: this project has no plan — call orchestrate first."
    why = "No wait requested."
    if wait and running:
        start = _fingerprint(plan, running)
        deadline = time.monotonic() + wait
        why = f"Waited {wait}s; nothing changed."
        while time.monotonic() < deadline:
            if await _pending_for(cid):
                why = "A message arrived for you; you get it right after this result."
                break
            await asyncio.sleep(min(STATUS_POLL_SECONDS,
                                    max(0.0, deadline - time.monotonic())))
            plan, running = load(slug) or plan, is_running(slug)
            if _fingerprint(plan, running) != start:
                why = "The plan changed." if running else "The run finished."
                break
    rollup = None if running else await _head_rollup(plan.get("root_id"))
    return _status_text(plan, running, rollup, why)
