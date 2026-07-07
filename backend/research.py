"""Search-and-divide research — a deterministic pipeline, not a free-looping
ReAct agent (that was what burned 5M tokens re-sending a snowballing context).

  1. Scout  — generate a batch of queries, run them all, collect one
              deduplicated result list (snippets only; cheap), then filter it
              down to the good, diverse sources and split them into groups.
  2. Readers — one per group, in parallel. Each fetches its assigned pages and
               SUMMARIZES each immediately (compaction), so it carries tight
               summaries, never raw pages. Nothing snowballs; no page is read
               twice (the list was pre-deduped and assignments are disjoint).
  3. Synthesize — the summaries become one cited document, staged for approval.

Every phase publishes bus events so the whole thing streams live on the Runs
tab (head -> scout -> readers). Token cost is bounded and predictable.
"""
import asyncio
import json
import re
import uuid
from datetime import date
from urllib.parse import urlparse

from . import bus, staging, webtools
from .agent import budget as budget_mod
from .agent.loop import _enforce_rules
from .agent.model import confirm_peak, model
from .config import settings
from .db import get_db
from .memory import standing_rules_tail
from .staging import stage_write

MAX_QUERIES = 8
RESULTS_PER_QUERY = 6
MAX_SOURCES_TO_FILTER = 40
MAX_URLS_PER_READER = 4


async def _complete_text(system: str, user: str, temperature: float = 0.3) -> str:
    parts = []
    async for ev in model.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature):
        if ev["type"] == "message":
            parts.append(ev["content"])
    return "".join(parts).strip()


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "topic"


def _dom(u: str) -> str:
    try:
        return urlparse(u).netloc.replace("www.", "") or u
    except Exception:
        return u


def _write_doc(project: str, doc_path: str, doc: str) -> str:
    """Write the FINAL research document: stage it, and when the operator has
    auto-approve on (the default) promote it straight to canonical. Node
    scratch files under runs/ stay staged either way — only the synthesized
    doc is trusted enough to skip the queue. Returns 'canonical'|'staged'."""
    stage_write(project, doc_path, doc.encode())
    if settings.research_auto_approve:
        staging.approve(project, [doc_path])
        return "canonical"
    return "staged"


async def _node(project, parent, job_id, kind, title) -> int:
    db = await get_db()
    try:
        pid = None
        if project:
            async with db.execute("SELECT id FROM projects WHERE slug = ?", (project,)) as cur:
                row = await cur.fetchone()
            pid = row["id"] if row else None
        cur = await db.execute(
            "INSERT INTO conversations (project_id, summary, kind, parent_conversation_id, job_id) "
            "VALUES (?, ?, ?, ?, ?)", (pid, f"[{kind}] {title[:60]}", kind, parent, job_id))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _save_rollup(cid: int, rollup: str) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE conversations SET rollup = ? WHERE id = ?", (rollup, cid))
        await db.commit()
    finally:
        await db.close()


# --- phase 1: scout ----------------------------------------------------------

async def _gen_queries(topic: str) -> list[str]:
    text = await _complete_text(
        f"Generate up to {MAX_QUERIES} diverse web search queries that together "
        "cover this research topic well (different angles, not rephrasings). One "
        "query per line, no numbering, no preamble.", f"Topic: {topic}")
    qs = [ln.strip("-*0123456789. ").strip() for ln in text.splitlines() if ln.strip()]
    return qs[:MAX_QUERIES]


async def _batch_search(queries: list[str]) -> list[dict]:
    # a transient SearXNG hiccup would otherwise yield 0 sources and a silently
    # empty research, so retry the whole batch once before giving up
    for attempt in range(2):
        lists = await asyncio.gather(
            *[webtools.search_results(q, limit=RESULTS_PER_QUERY) for q in queries],
            return_exceptions=True)
        seen, out = set(), []
        for lst in lists:
            if not isinstance(lst, list):
                continue
            for r in lst:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    out.append(r)
        if out or attempt == 1:
            return out[:MAX_SOURCES_TO_FILTER]
        await asyncio.sleep(1.5)
    return []


def _parse_groups(text: str, valid_urls: set) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    groups = []
    for g in data if isinstance(data, list) else []:
        urls = [u for u in (g.get("urls") or []) if u in valid_urls][:MAX_URLS_PER_READER]
        if urls:
            groups.append({"theme": (g.get("theme") or "sources")[:80], "urls": urls})
    return groups


async def _filter_and_assign(topic: str, results: list[dict], n_groups: int) -> list[dict]:
    if not results:
        return []
    listing = "\n".join(
        f"{i}. {r['title']} — {r['url']}\n   {r['snippet']}" for i, r in enumerate(results))
    text = await _complete_text(
        f"From the search results below for the topic '{topic}', pick the most "
        f"relevant and DIVERSE sources worth reading (about {n_groups * 3} total). "
        "Discard duplicates, low-quality, and off-topic results. Group the kept "
        f"URLs into {n_groups} themed reading groups. Reply ONLY with JSON: "
        '[{"theme": "...", "urls": ["...", "..."]}]', listing, temperature=0.2)
    valid = {r["url"] for r in results}
    groups = _parse_groups(text, valid)
    if not groups:  # fallback: just split the top results evenly
        top = [r["url"] for r in results[:n_groups * 3]]
        groups = [{"theme": topic, "urls": top[i::n_groups]} for i in range(n_groups)]
        groups = [g for g in groups if g["urls"]]
    return groups


# --- phase 2: readers (compaction) -------------------------------------------

async def _summarize_page(topic: str, theme: str, url: str, text: str) -> str:
    return await _complete_text(
        f"Summarize what this page says that is relevant to '{theme}' (overall "
        f"topic: {topic}) in 3-5 tight bullet points. Only facts stated on the "
        "page. No preamble.", f"URL: {url}\n\n{text[:settings.web_max_chars]}")


async def _reader(project, parent, job_id, group, topic, session) -> str:
    cid = await _node(project, parent, job_id, "reader", group["theme"])
    bus.publish(job_id, {"type": "node_spawned", "node_id": cid, "parent_id": parent,
                         "kind": "reader", "title": group["theme"], "depth": 1})
    bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "running"})
    summaries = []
    for url in group["urls"]:
        bus.publish(job_id, {"type": "tool", "node_id": cid, "name": f"read {_dom(url)}"})
        text = await webtools.read(url, session)
        if text.startswith("error") or text.startswith("note"):
            continue
        summary = await _summarize_page(topic, group["theme"], url, text)  # compaction
        summaries.append(f"Source: {url}\n{summary}")
    findings = (f"## {group['theme']}\n\n" +
                ("\n\n".join(summaries) if summaries else "(no usable sources)"))
    bus.publish(job_id, {"type": "node_status", "node_id": cid, "status": "summarizing"})
    await _save_rollup(cid, findings[:3000])
    stage_write(project, f"runs/{job_id}/{cid}-reader.md", findings.encode())
    bus.publish(job_id, {"type": "node_done", "node_id": cid, "rollup": findings[:3000]})
    return findings


# --- phase 3: synthesize -----------------------------------------------------

async def _synthesize(topic: str, findings: list[str]) -> str:
    joined = "\n\n".join(findings)
    body = await _complete_text(
        "Synthesize the research findings below into one clean, well-structured "
        "markdown document: a short intro, clear sections, and a final 'Sources' "
        "list of the URLs cited. Use only what the findings support. No preamble.",
        f"Topic: {topic}\n\nFindings:\n\n{joined}")
    doc = (f"# Research: {topic}\n\n*Compiled {date.today().isoformat()} by Jarvis "
           "research agents.*\n\n" + body)
    rules = standing_rules_tail()
    return await _enforce_rules(doc, rules) if rules else doc


# --- the pipeline ------------------------------------------------------------

async def run_research(topic: str, project: str, n_angles: int = 3,
                       job_id: str | None = None) -> dict:
    n_groups = max(2, min(4, n_angles))
    job_id = job_id or uuid.uuid4().hex
    doc_path = f"research/{_slugify(topic)}.md"

    tok = None
    b = budget_mod.active_budget.get()
    if b is None:
        b = budget_mod.Budget(settings.max_op_input_tokens, settings.max_op_output_tokens)
        tok = budget_mod.active_budget.set(b)
    try:
        head = await _node(project, None, job_id, "head", f"Research: {topic}")
        confirm_peak(head)
        bus.publish(job_id, {"type": "job_start", "job_id": job_id, "root_id": head})
        bus.publish(job_id, {"type": "node_spawned", "node_id": head, "parent_id": None,
                             "kind": "head", "title": f"Research: {topic}", "depth": 0})

        # phase 1: scout
        scout = await _node(project, head, job_id, "scout", "search & filter")
        confirm_peak(scout)
        bus.publish(job_id, {"type": "node_spawned", "node_id": scout, "parent_id": head,
                             "kind": "scout", "title": "search & filter", "depth": 1})
        bus.publish(job_id, {"type": "node_status", "node_id": scout, "status": "running"})
        queries = await _gen_queries(topic)
        for q in queries:
            bus.publish(job_id, {"type": "tool", "node_id": scout, "name": f"search: {q}"})
        results = await _batch_search(queries)
        bus.publish(job_id, {"type": "node_status", "node_id": scout, "status": "summarizing"})
        groups = await _filter_and_assign(topic, results, n_groups)
        kept = sum(len(g["urls"]) for g in groups)
        scout_rollup = (f"Ran {len(queries)} searches, found {len(results)} unique "
                        f"sources, kept {kept} across {len(groups)} reading groups.")
        await _save_rollup(scout, scout_rollup)
        stage_write(project, f"runs/{job_id}/{scout}-scout.md", scout_rollup.encode())
        bus.publish(job_id, {"type": "node_done", "node_id": scout, "rollup": scout_rollup})

        if not groups:
            # no sources -> say so plainly instead of synthesizing from nothing
            doc = (f"# Research: {topic}\n\nThe search backend returned no usable "
                   "sources for this topic (it may have been momentarily "
                   "unavailable). Please try again.")
            doc_status = _write_doc(project, doc_path, doc)
            note = "no sources retrieved — search returned nothing"
            await _save_rollup(head, note)
            bus.publish(job_id, {"type": "job_final", "job_id": job_id, "root_id": head,
                                 "doc_path": doc_path, "rollup": note, "usage": b.summary()})
            bus.close_job(job_id)
            return {"topic": topic, "job_id": job_id, "root_id": head,
                    "doc_path": doc_path, "doc_status": doc_status}

        # phase 2: readers in parallel (session = job_id so reads are fresh per run)
        findings = await asyncio.gather(
            *[_reader(project, head, job_id, g, topic, job_id) for g in groups])

        # phase 3: synthesize
        doc = await _synthesize(topic, [f for f in findings if f])
        doc_status = _write_doc(project, doc_path, doc)
        head_rollup = f"Researched '{topic}' via {len(groups)} reader groups. {b.summary()}"
        await _save_rollup(head, head_rollup)
        stage_write(project, f"runs/{job_id}/{head}-head.md", head_rollup.encode())

        bus.publish(job_id, {"type": "job_final", "job_id": job_id, "root_id": head,
                             "doc_path": doc_path, "rollup": head_rollup,
                             "usage": b.summary()})
        bus.close_job(job_id)
        return {"topic": topic, "job_id": job_id, "root_id": head,
                "doc_path": doc_path, "doc_status": doc_status}
    finally:
        if tok is not None:
            budget_mod.active_budget.reset(tok)
