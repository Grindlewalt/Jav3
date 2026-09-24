"""Agent definitions: agents/<slug>/AGENT.md — frontmatter + system prompt.

The exclusion model is deliberate: an agent gets EVERYTHING (context files,
tools, skills) by default and lists what to remove, so necessary pieces
can't be forgotten — only knowingly taken away. Running them lives in
agents_run.py (one-shot runs, spawn_agent, schedules) and chat.py (an agent
chat thread: `conversations.agent_slug`).
"""
import re

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .agent.tools.registry import _parse_md
from .auth import require_user
from .config import settings

router = APIRouter(prefix="/api/agents", tags=["agents"],
                   dependencies=[Depends(require_user)])

# CO-STAR-shaped default: an agent prompt has no per-turn size cap (unlike
# tool specs), so explicit structure is cheap and measurably steadies flash.
# The GUI generator (GENERATE_SYSTEM) fills the same skeleton.
DEFAULT_PROMPT = """# Context
You are {name}, an agent working for the operator inside Jarvis. You run
headless when spawned: there are no follow-up questions, so decide and act
on the brief you were given.

# Objective
(What this agent is for, its exact scope, and what "done" looks like.
Edit me.)

# Style
Work in the smallest number of tool calls that does the job. When something
is outside your scope or cannot be determined, say so instead of guessing.

# Tone
Direct and factual. No filler, no hedging.

# Audience
The operator, or a head agent that will synthesize your report with others —
assume your reply is read once, fast.

# Response
Report back concisely: lead with the outcome, then only the details that
change what the reader does next.
"""

FIELD_DEFAULTS = {
    "description": "",
    "model": "",          # "" = inherit the main model (deepseek-flash)
    "base_url": "",       # "" = default DeepSeek endpoint; e.g. ollama: http://localhost:11434/v1
    # memory_read/memory_write use agents/<slug>/memory/ instead of the shared
    # memory/notes/ whenever a turn runs as this agent (memory.notes_dir via
    # runtime.agent_memory). The shared standing notes still lead its prompt —
    # the operator's rules are not optional — this only redirects its OWN notes.
    "own_memory": False,
    # subtractive model: everything by default. The GUI no longer edits these,
    # but they stay honoured server-side (the funnel and hand-edited AGENT.md
    # files may set them). skills_exclude unions with tools_exclude at run time
    # (agents_run.agent_exclusions) — skills compile into the same registry.
    "context_exclude": [],
    "tools_exclude": [],
    "skills_exclude": [],
    # rounds of the ReAct loop this agent may take. 0 = the path's default: the
    # tight subagent cap for headless runs (spawn_agent, schedules), the full
    # chat cap for a run or thread the operator started and is watching.
    "max_iterations": 0,
    # the project this agent lives in ("" = none: follow the caller). Run-time
    # precedence is request `project` > this > the caller's pin/global active
    # (agents_run.resolve_run_project) — for headless, interactive, spawned
    # runs and agent chat threads alike.
    "project": "",
}


class CreateAgent(BaseModel):
    name: str


# --- auto prompt generator: one-line description -> quiz -> system prompt ----

QUIZ_SYSTEM = """You design system prompts for task agents. Given a one-line \
description of an agent, produce 3-5 short clarifying questions whose answers \
would most improve the prompt — scope, tone, output format, autonomy/limits, \
failure behavior. Reply with ONLY a JSON array, no prose:
[{"question": "...", "kind": "single"|"multi"|"short", "options": ["...", ...]}]
kind "short" means free text (options must be []). Keep options concrete and \
mutually distinct, 2-4 per question."""

GENERATE_SYSTEM = """You write system prompts for task agents. Given the \
agent's description and the operator's answers to clarifying questions, write \
a complete system prompt (150-300 words) in direct second person ("You \
are..."), structured with EXACTLY these markdown headings, in this order:

# Context — who the agent is and where it runs (headless, no follow-up \
questions, acts on the brief it is given)
# Objective — its exact scope, what "done" looks like, and what it must \
NOT do
# Style — how it works: method, sources/tools to prefer, how to handle \
failure or out-of-scope requests
# Tone — the voice of its output
# Audience — who reads the result and what they need from it
# Response — the exact output format: sections, length cap, what to \
include when something could not be determined

Fill every section with content specific to THIS agent — no placeholder \
text. Output only the prompt text, no preamble or fences."""


class QuizRequest(BaseModel):
    description: str


class GenerateRequest(BaseModel):
    description: str
    answers: list[dict] = []   # [{question, answer}]


def _extract_json_array(text: str):
    import json
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text, flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("no JSON array in reply")
    return json.loads(text[start:end + 1])


@router.post("/prompt-quiz")
async def prompt_quiz(body: QuizRequest):
    from .summarize import complete_text
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="description is required")
    last_err = None
    for attempt in range(2):
        try:
            raw = await complete_text(
                QUIZ_SYSTEM if attempt == 0 else
                QUIZ_SYSTEM + "\nYour previous reply was not valid JSON. "
                "Reply with ONLY the JSON array.",
                f"Agent description: {body.description.strip()}",
                temperature=0.4)
            questions = _extract_json_array(raw)
            cleaned = [{"question": str(q.get("question", "")).strip(),
                        "kind": q.get("kind") if q.get("kind") in
                        ("single", "multi", "short") else "single",
                        "options": [str(o) for o in (q.get("options") or [])]}
                       for q in questions if str(q.get("question", "")).strip()]
            if cleaned:
                return {"questions": cleaned[:5]}
            last_err = "model returned no questions"
        except Exception as e:  # noqa: BLE001 — surfaced as a 502 below
            last_err = str(e)
    raise HTTPException(status_code=502, detail=f"quiz generation failed: {last_err}")


@router.post("/prompt-generate")
async def prompt_generate(body: GenerateRequest):
    from .summarize import complete_text
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="description is required")
    answered = "\n".join(
        f"Q: {a.get('question', '')}\nA: {a.get('answer', '')}"
        for a in body.answers if str(a.get('answer', '')).strip())
    try:
        prompt = await complete_text(
            GENERATE_SYSTEM,
            f"Agent description: {body.description.strip()}\n\n"
            f"Operator's answers:\n{answered or '(none given)'}",
            temperature=0.5)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"generation failed: {e}")
    if not prompt.strip():
        raise HTTPException(status_code=502, detail="model returned an empty prompt")
    return {"prompt": prompt.strip()}


class SaveAgent(BaseModel):
    name: str
    description: str = ""
    model: str = ""
    base_url: str = ""
    own_memory: bool = False
    context_exclude: list[str] = []
    tools_exclude: list[str] = []
    skills_exclude: list[str] = []
    max_iterations: int = 0
    project: str = ""
    prompt: str = ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="name produces empty slug")
    return slug


def _agent_path(slug: str):
    return settings.agents_dir / slug / "AGENT.md"


def _write(slug: str, body: SaveAgent) -> None:
    meta = {"name": body.name, **{k: getattr(body, k) for k in FIELD_DEFAULTS}}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    path = _agent_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n\n{body.prompt.strip()}\n")


def _read(slug: str) -> dict:
    path = _agent_path(slug)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    # relaxed requirements: an AGENT.md with no `description:` is odd but
    # usable, and 500ing on it made an agent the roster happily lists
    # un-openable — and un-runnable as a chat identity too
    meta = _parse_md(path, required=())
    if meta is None:
        raise HTTPException(status_code=500, detail="unparseable AGENT.md")
    out = {"slug": slug, "name": meta.get("name", slug), "prompt": meta.get("body", "")}
    for key, default in FIELD_DEFAULTS.items():
        out[key] = meta.get(key, default)
    return out


def _list_dir(base):
    agents = []
    if base.exists():
        for md in sorted(base.glob("*/AGENT.md")):
            if md.parent.name.startswith("."):
                continue  # skip the .trash bin
            meta = _parse_md(md, required=()) or {}
            agents.append({
                "slug": md.parent.name,
                "name": meta.get("name", md.parent.name),
                "description": meta.get("description", ""),
                "model": meta.get("model", ""),
                "project": meta.get("project") or "",
            })
    return agents


@router.get("")
async def list_agents():
    return {"agents": _list_dir(settings.agents_dir)}


@router.get("/trash")
async def list_trash():
    return {"agents": _list_dir(settings.agents_dir / ".trash")}


# Every agent's outputs in one query, for the Outputs tab's all-agents view
# (one request instead of one per roster entry). Same tree walk as
# agents_run._OUTPUTS_SQL, seeded by EVERY stamped conversation and carrying
# the root's slug down as `owner`. The walk stops at a descendant that is
# itself stamped: that one is its own agent's output and seeds its own branch,
# so no row appears twice. UNION over (id, owner) pairs ends a parent cycle.
_ALL_OUTPUTS_SQL = """
WITH RECURSIVE tree(id, owner) AS (
    SELECT id, agent_slug FROM conversations
     WHERE agent_slug IS NOT NULL AND agent_slug != ''
    UNION
    SELECT c.id, t.owner FROM conversations c JOIN tree t
        ON c.parent_conversation_id = t.id
     WHERE c.agent_slug IS NULL OR c.agent_slug = ''
)
SELECT c.id, c.kind, c.summary AS title, c.agent_slug, t.owner,
       c.parent_conversation_id AS parent_id, c.job_id, c.rollup, c.started_at,
       p.slug AS project,
       (SELECT m.content FROM messages m WHERE m.conversation_id = c.id
         ORDER BY m.id DESC LIMIT 1) AS last_message,
       (SELECT MAX(m.created_at) FROM messages m
         WHERE m.conversation_id = c.id) AS last_at
FROM conversations c JOIN tree t ON t.id = c.id
LEFT JOIN projects p ON p.id = c.project_id
ORDER BY c.started_at DESC, c.id DESC
LIMIT ?
"""


# Declared before the `/{slug}` catch-all, which would otherwise read
# "outputs" as an agent slug and 404.
@router.get("/outputs")
async def all_agent_outputs(limit: int = 50):
    """GET /api/agents/{slug}/outputs for every agent at once: the same row
    shape plus `owner`, the agent whose work the row is (a funnel node or a
    temp agent is unstamped, so its owner is the stamped ancestor it descends
    from). A deleted agent's rows stay — past work is still past work."""
    from .agents_run import _active_runs, _runs_files
    from .chat import _active_turns
    from .db import get_db
    db = await get_db()
    try:
        async with db.execute(_ALL_OUTPUTS_SQL, (max(1, min(limit, 200)),)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    for r in rows:
        r["snippet"] = " ".join((r.pop("last_message") or "").split())[:240]
        r["running"] = r["id"] in _active_runs or r["id"] in _active_turns
        r["runs_files"] = _runs_files(r["project"], r["job_id"])
    return {"outputs": rows}


@router.post("")
async def create_agent(body: CreateAgent):
    slug = _slugify(body.name)
    if _agent_path(slug).exists():
        raise HTTPException(status_code=409, detail=f"agent '{slug}' already exists")
    _write(slug, SaveAgent(name=body.name, prompt=DEFAULT_PROMPT.format(name=body.name)))
    return {"slug": slug}


@router.get("/{slug}")
async def read_agent(slug: str):
    return _read(slug)


async def _check_project(slug: str) -> None:
    """A definition's project must be a live project (or empty). Checked at save
    so a typo is a 400 in the editor, not a failed run at 06:45; a project
    deleted AFTER the save is caught again at run time."""
    if not slug:
        return
    from .db import get_db
    db = await get_db()
    try:
        async with db.execute(
            "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
            (slug,)) as cur:
            ok = await cur.fetchone() is not None
    finally:
        await db.close()
    if not ok:
        raise HTTPException(status_code=400, detail=f"no such project: {slug}")


@router.put("/{slug}")
async def save_agent(slug: str, body: SaveAgent):
    if not _agent_path(slug).is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    body.project = body.project.strip()
    await _check_project(body.project)
    _write(slug, body)
    return {"ok": True}


@router.delete("/{slug}")
async def delete_agent(slug: str):
    """Soft delete: move to the .trash bin. Restorable until purged."""
    import shutil
    path = _agent_path(slug)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such agent")
    trash = settings.agents_dir / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / slug
    if dest.exists():
        shutil.rmtree(dest)  # a re-created-then-deleted agent overwrites its old grave
    shutil.move(str(path.parent), str(dest))
    return {"ok": True}


@router.post("/{slug}/restore")
async def restore_agent(slug: str):
    import shutil
    src = settings.agents_dir / ".trash" / slug
    if not (src / "AGENT.md").is_file():
        raise HTTPException(status_code=404, detail="not in the trash")
    dest = settings.agents_dir / slug
    if dest.exists():
        raise HTTPException(status_code=409,
                            detail=f"an agent '{slug}' already exists — rename it first")
    shutil.move(str(src), str(dest))
    return {"ok": True}


@router.delete("/{slug}/purge")
async def purge_agent(slug: str):
    """Permanent: only from the trash."""
    import shutil
    src = settings.agents_dir / ".trash" / slug
    if not (src / "AGENT.md").is_file():
        raise HTTPException(status_code=400, detail="delete first — purge only empties trash")
    shutil.rmtree(src)
    return {"ok": True}
