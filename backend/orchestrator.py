"""The Agentic Context Funnel (M7): a recursive agent hierarchy.

central -> head -> (task leader) -> subagent. Context narrows going down (each
layer gets less than the one above); tight roll-up summaries flow back up into
a walkable tree (each node is a `conversations` row with a `rollup`, linked by
`parent_conversation_id`). The same `run_turn` primitive runs every node.

A node either DECOMPOSES (a head/leader splitting work into child nodes it runs
in parallel and then assembles) or runs DIRECT (does the work itself via
run_turn). Subagents are always terminal. Hard caps on depth, total nodes, and
fan-out keep cost bounded; over-budget decomposition degrades to DIRECT rather
than erroring, so a job always finishes with a rollup.

Live progress streams through the in-process bus (backend/bus.py): every node
lifecycle + tool/token event is published under the job id for the SSE view.
"""
import asyncio

from . import bus
from .agent.agent import Agent
from .agent.loop import run_turn
from .agent.model import confirm_peak, model
from .db import get_db
from .memory import assemble_system_prompt
from .staging import stage_write

MAX_DEPTH = 2      # head=0 -> leader=1 -> subagent=2 (terminal)
MAX_NODES = 24     # total nodes per job
MAX_FANOUT = 6     # children per decomposing node

SUBAGENT_PROMPT = """You are a subagent with one narrow job: do exactly the task
you are given, using only the tools provided, and report a concise result. Stay
strictly on task; do not attempt anything outside it."""


class _Budget:
    """Shared across a whole job; bounds total node count."""
    def __init__(self, n: int):
        self.n = n

    def take(self) -> bool:
        if self.n > 0:
            self.n -= 1
            return True
        return False

    def remaining(self) -> int:
        return self.n


async def _complete_text(system: str, user: str, temperature: float = 0.3) -> str:
    parts = []
    async for ev in model.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature,
    ):
        if ev["type"] == "message":
            parts.append(ev["content"])
    return "".join(parts).strip()


async def _decompose(brief: str, kind: str, hint: str | None) -> list[dict]:
    """Ask whether this node's work should be split. Returns a list of
    {kind, title} children, or [] to run DIRECT. Head children may be leaders
    (complex) or subagents (simple); leader children are always subagents."""
    allow_leader = kind == "head"
    kinds = ("LEADER: <task> (needs further breakdown) or SUBAGENT: <task> (one narrow task)"
             if allow_leader else "SUBAGENT: <task>")
    plan = await _complete_text(
        f"You are a {kind} planner. Decide if the work below should be broken "
        f"into pieces. If it is genuinely simple, reply with only the word "
        f"DIRECT. Otherwise reply one piece per line as:\n{kinds}\n"
        f"{hint or ''} Keep pieces distinct and non-overlapping.",
        brief)
    if "DIRECT" in plan.split("\n")[0].upper() and len(plan.splitlines()) <= 1:
        return []
    out = []
    for ln in plan.splitlines():
        ln = ln.strip("-*0123456789. ").strip()
        if not ln:
            continue
        up = ln.upper()
        if up.startswith("LEADER:") and allow_leader:
            out.append({"kind": "leader", "title": ln.split(":", 1)[1].strip()})
        elif up.startswith("SUBAGENT:"):
            out.append({"kind": "subagent", "title": ln.split(":", 1)[1].strip()})
        elif ln and up != "DIRECT":
            out.append({"kind": "subagent", "title": ln})
    return out[:MAX_FANOUT]


async def _rollup(brief: str, output: str) -> str:
    """A tight process.md: what this node did and the result the next node needs."""
    return await _complete_text(
        "Summarize this agent's work into a few tight lines (a process.md): what "
        "it did, the key result/answer, and any sources or pointers the next "
        "agent needs. No preamble.",
        f"Task: {brief}\n\nOutput:\n{output[:6000]}")


async def _open_child(db, parent_cid: int, job_id: str, project: str,
                      kind: str, title: str) -> int:
    pid = None
    if project:
        async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
            row = await cur.fetchone()
        pid = row["id"] if row else None
    cur = await db.execute(
        "INSERT INTO conversations (project_id, summary, kind, parent_conversation_id, job_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (pid, f"[{kind}] {title[:60]}", kind, parent_cid, job_id))
    await db.commit()
    return cur.lastrowid


async def _node_context(kind: str, project: str, parent_summary: str) -> str:
    """The funnel narrowing. Subagents get a minimal fixed context; head/leader
    nodes that run DIRECT get the full central context."""
    if kind == "subagent":
        ctx = SUBAGENT_PROMPT
        if parent_summary:
            ctx += f"\n\n# Context from your lead\n{parent_summary}"
        return ctx
    db = await get_db()
    try:
        return await assemble_system_prompt(db, active=project or None)
    finally:
        await db.close()


async def run_node(*, job_id: str, cid: int, kind: str, brief: str, project: str,
                   depth: int, budget: _Budget, leaf_tools, peak: bool,
                   parent_summary: str = "", deliverable=None) -> dict:
    """Run one node. Returns {"cid","kind","output","rollup"}."""
    doc_path = None
    try:
        bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "planning"})
        subtasks = []
        if kind in ("head", "leader") and depth < MAX_DEPTH and budget.remaining() > 0:
            subtasks = await _decompose(brief, kind, None)

        if subtasks:
            # spawn children in parallel — this is the tree lighting up live
            children = []
            db = await get_db()
            try:
                for st in subtasks:
                    if not budget.take():
                        break
                    child_cid = await _open_child(db, cid, job_id, project,
                                                  st["kind"], st["title"])
                    if peak:
                        confirm_peak(child_cid)
                    bus.publish(job_id, {
                        "type": "node_spawned", "node_id": child_cid, "parent_id": cid,
                        "kind": st["kind"], "title": st["title"], "depth": depth + 1})
                    children.append((child_cid, st))
            finally:
                await db.close()
            bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "delegating"})
            results = await asyncio.gather(*[
                run_node(job_id=job_id, cid=ccid, kind=st["kind"], brief=st["title"],
                         project=project, depth=depth + 1, budget=budget,
                         leaf_tools=leaf_tools, peak=peak, parent_summary=brief)
                for ccid, st in children], return_exceptions=True)
            child_outputs = [r["output"] for r in results if isinstance(r, dict)]
            child_rollups = [r["rollup"] for r in results if isinstance(r, dict)]
            # summaries flow up: this node's rollup is a summary of its children's
            rollup_source = "\n\n".join(child_rollups)
            # the head's deliverable (e.g. the research doc) returns a staged path
            if deliverable is not None:
                doc_path = await deliverable(child_outputs)
                output = doc_path
            else:
                output = "\n\n".join(child_outputs)  # a leader hands work up
        else:
            # DIRECT / subagent — do the work through the shared loop.
            # Build the node via the Agent seam: narrowed context + this layer's
            # brief. spawn() is what a parent used to mint this child's context.
            bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "running"})
            context = await _node_context(kind, project, parent_summary)
            node = Agent(context=context, tools=leaf_tools or [], brief=brief)
            system_prompt = node.system_prompt()
            db = await get_db()
            try:
                final = ""
                async for ev in run_turn(db, cid, system_prompt,
                                         [{"role": "user", "content": brief}],
                                         tools=leaf_tools, self_check=False):
                    if ev["type"] in ("token", "tool"):
                        bus.publish(job_id, {**ev, "node_id": cid})
                    elif ev["type"] == "final":
                        final = ev["content"]
            finally:
                await db.close()
            output = final
            rollup_source = final

        bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "summarizing"})
        rollup = await _rollup(brief, rollup_source)
        db = await get_db()
        try:
            await db.execute("UPDATE conversations SET rollup = ? WHERE id = ?", (rollup, cid))
            await db.commit()
        finally:
            await db.close()
        stage_write(project, f"runs/{job_id}/{cid}-{kind}.md", rollup.encode())
        bus.publish(job_id, {"type": "node_done", "node_id": cid, "rollup": rollup})
        return {"cid": cid, "kind": kind, "output": output, "rollup": rollup,
                "doc_path": doc_path}
    except Exception as e:  # noqa: BLE001 — a node failure must not kill siblings
        bus.publish(job_id, {"type": "error", "node_id": cid, "message": str(e)})
        return {"cid": cid, "kind": kind, "output": "", "rollup": f"error: {e}",
                "doc_path": None}


async def run_job(job_id: str, brief: str, project: str, *, peak: bool = False,
                  leaf_tools=None, deliverable=None, title: str = "") -> dict:
    """Open the head node, run the tree, publish job lifecycle events. Returns
    {"root_id","rollup","doc_path"}. `deliverable(child_outputs)->str` builds
    the head's final product; `_stage_deliverable` stages it if a path is set."""
    db = await get_db()
    try:
        pid = None
        if project:
            async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
                row = await cur.fetchone()
            pid = row["id"] if row else None
        cur = await db.execute(
            "INSERT INTO conversations (project_id, summary, kind, job_id) "
            "VALUES (?, ?, 'head', ?)", (pid, f"[head] {(title or brief)[:60]}", job_id))
        root_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()

    if peak:
        confirm_peak(root_id)
    bus.publish(job_id, {"type": "job_start", "job_id": job_id, "root_id": root_id})
    bus.publish(job_id, {
        "type": "node_spawned", "node_id": root_id, "parent_id": None,
        "kind": "head", "title": title or brief[:60], "depth": 0})

    budget = _Budget(MAX_NODES)
    budget.take()  # the head itself
    result = await run_node(job_id=job_id, cid=root_id, kind="head", brief=brief,
                            project=project, depth=0, budget=budget,
                            leaf_tools=leaf_tools, peak=peak, deliverable=deliverable)

    bus.publish(job_id, {"type": "job_final", "job_id": job_id, "root_id": root_id,
                         "rollup": result["rollup"], "doc_path": result.get("doc_path")})
    bus.close_job(job_id)
    return {"root_id": root_id, "rollup": result["rollup"],
            "doc_path": result.get("doc_path")}
