"""The Tools page shows tools the way the model sees them (tool cleanup, step 1):
GET /api/tools says per built-in tool its section, its action in the merged tool,
core, merged_into, internal and gating; the four harness-only tools carry
`internal: true` without any change to what is granted; docs/tool-map.md is
generated from the same data. No behaviour changes: nothing here may touch what
the model is offered."""
import json
from pathlib import Path

import httpx
import pytest

from backend import toolcatalog
from backend.agent.tools import registry, toolsections
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

ROOT = Path(__file__).resolve().parent.parent
INTERNAL = {"plan_status", "plan_fix", "plan_report", "inbox_fetch"}


@pytest.fixture
async def client(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


async def _builtin(client) -> dict:
    body = (await client.get("/api/tools")).json()
    return {t["name"]: t for t in body["tools"] if t["group"] == "builtin"}


async def test_every_builtin_row_says_how_the_model_sees_it(client):
    rows = await _builtin(client)
    folders = {p.parent.name for p in (ROOT / "tools").glob("*/TOOL.md")}
    assert set(rows) == folders
    for n, t in rows.items():
        assert {"section", "action", "core", "merged_into", "internal", "gating"} <= set(t), n
        assert isinstance(t["gating"], list) and isinstance(t["internal"], bool), n
        # a folded tool has both a merged name and an action name there; a tool
        # shown by its own name has neither
        assert (t["merged_into"] is None) == (t["action"] is None), n
        assert t["section"] in toolsections.SECTIONS, n


async def test_folded_and_standalone_tools(client):
    rows = await _builtin(client)
    click = rows["browser_click"]
    assert (click["section"], click["merged_into"], click["action"]) == ("browser", "browser", "click")
    assert not click["core"] and click["gating"] == ["needs extension"]
    # a merged tool is core when any of its actions is (web: all of them are)
    assert rows["web_read"]["merged_into"] == "web" and rows["web_read"]["core"]
    # a core tool the model sees by its own name
    rf = rows["read_file"]
    assert rf["merged_into"] is None and rf["action"] is None and rf["core"]
    assert rf["gating"] == ["in-guest", "needs project"]
    # a member that takes an `action` of its own still folds (as `do`)
    assert rows["music_control"]["merged_into"] == "media"
    assert rows["music_control"]["action"] == "control"


async def test_gating_labels(client):
    rows = await _builtin(client)
    assert "needs desk" in rows["desk_click"]["gating"]
    assert rows["desk_shell"]["gating"] == ["needs desk", "needs shell"]
    assert rows["local_shell"]["gating"] == ["local chats only"]
    assert rows["plan_report"]["gating"] == ["plan items only"]
    assert rows["inbox_fetch"]["gating"] == ["harness only"]
    assert rows["play_music"]["gating"] == ["disabled"]
    assert "needs setup" in rows["projector_show"]["gating"]
    assert rows["web_search"]["gating"] == []
    assert "in-guest" in rows["run_code"]["gating"]


async def test_rows_come_in_the_models_order_with_the_sections_described(client):
    body = (await client.get("/api/tools")).json()
    builtin = [t for t in body["tools"] if t["group"] == "builtin"]
    sections = [s["name"] for s in body["sections"]]
    assert sections == [s for s in toolsections.SECTIONS if s in {t["section"] for t in builtin}]
    assert [t["section"] for t in builtin] == sorted(
        (t["section"] for t in builtin), key=sections.index)
    assert all(isinstance(s["about"], str) for s in body["sections"])
    # the actions of one merged tool sit together, the first-step actions first
    browser = [t["action"] for t in builtin if t["merged_into"] == "browser"]
    assert len(browser) > 5 and browser[:4] == ["read", "open_tab", "list_tabs", "navigate"]
    ix = [i for i, t in enumerate(builtin) if t["merged_into"] == "browser"]
    assert ix == list(range(ix[0], ix[0] + len(ix)))


async def test_only_the_harness_tools_are_internal(client):
    rows = await _builtin(client)
    assert {n for n, t in rows.items() if t["internal"]} == INTERNAL
    # the page's own switch (`enabled: false`) is unchanged for them
    assert all(not rows[n]["enabled"] for n in INTERNAL)


def test_internal_changes_nothing_the_model_is_offered():
    """`internal: true` is a label for the Tools page. Plan tools are still
    granted by the callers that run plan turns (they force `enabled`), the
    loop still dispatches inbox_fetch, and the key never reaches the spec."""
    entries = registry.load_registry()
    by = {e["name"]: e for e in entries}
    assert all(by[n].get("internal") is True and by[n]["enabled"] is False for n in INTERNAL)
    # every other tool is untouched
    assert not [e["name"] for e in entries if e.get("internal") and e["name"] not in INTERNAL]
    # a normal turn is not offered them
    offered = {s["function"]["name"] for s in registry.openai_tool_specs(entries)}
    assert not (INTERNAL & offered)
    # the grant chat.py gives an orchestrator, and agents_run gives a plan item
    granted = registry.openai_tool_specs([{**e, "enabled": True} for e in entries
                                          if e["name"] in INTERNAL - {"inbox_fetch"}])
    assert {s["function"]["name"] for s in granted} == INTERNAL - {"inbox_fetch"}
    assert all("internal" not in s and "internal" not in s["function"] for s in granted)
    assert all(s["section"] == "plans" and s["action"] for s in granted)
    from backend import agents_run
    assert {s["function"]["name"] for s in agents_run._internal_specs(("plan_report",))} == {"plan_report"}
    # the loop's mail check still finds a handler to run
    assert registry._load_dynamic("inbox_fetch") is not None


def test_the_catalogue_is_the_models_tool_list():
    """One card per tool as the model sees it: with every folder granted and
    every section loaded, the model's tool names are exactly the catalogue's
    tools (merged ones by their merged name) plus the section loader."""
    entries = [{**e, "enabled": True} for e in registry.load_registry() if e["kind"] == "tool"]
    specs = [s for s in registry.openai_tool_specs(
        [{k: v for k, v in e.items() if not k.startswith("requires_")} for e in entries])]
    view = toolsections.View(specs)
    view.loaded = set(toolsections.SECTIONS)
    shown = {s["function"]["name"] for s in view.wire()}
    cat = toolcatalog.catalogue(registry.load_registry())
    units = {r["merged_into"] or n for n, r in cat["rows"].items()}
    assert shown == units | {toolsections.META}
    tools, actions = toolcatalog.counts(cat, internal=True)
    assert (tools, actions) == (len(units), len(entries))
    assert toolcatalog.counts(cat) == (tools - 2, actions - len(INTERNAL))   # plans, inbox_fetch
    # every merged tool has at least two actions and no action twice
    merged = {}
    for r in cat["rows"].values():
        if r["merged_into"]:
            merged.setdefault(r["merged_into"], []).append(r["action"])
    assert all(len(a) > 1 and len(a) == len(set(a)) for a in merged.values())


def test_gating_reads_the_frontmatter():
    g = toolcatalog.gating
    assert g({"name": "x"}) == []
    assert g({"name": "x", "requires_project": True, "requires_browser": True}) == [
        "needs extension", "needs project"]
    assert g({"name": "x", "enabled": False, "section": "plans"}) == ["plan items only"]
    assert g({"name": "x", "enabled": False, "internal": True, "section": "system"}) == ["harness only"]
    assert g({"name": "x", "enabled": False}) == ["disabled"]
    assert g({"name": "write_file", "requires_project": True}) == ["in-guest", "needs project"]


def test_the_tool_map_doc_is_not_stale():
    """docs/tool-map.md maps every old (folder) name to the tool and action the
    model sees. Regenerate: python -m backend.toolcatalog --write"""
    doc = (ROOT / "docs" / toolcatalog.DOC_NAME).read_text()
    assert doc == toolcatalog.markdown(), "run: python -m backend.toolcatalog --write"
    for p in (ROOT / "tools").glob("*/TOOL.md"):
        assert f"| `{p.parent.name}` |" in doc, f"{p.parent.name} is missing from the map"
    assert '`browser(action="click")`' in doc and "`browser_click`" in doc


def test_the_catalogue_is_json():
    json.dumps(toolcatalog.catalogue(registry.load_registry()))
