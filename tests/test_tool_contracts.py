"""Every tool's promise matches its code, and a wrong argument costs no round.

2026-09-27: browser_read_page's TOOL.md offered wait_ms/min_elements/selector
its handler refused; play_music's TOOL.md was invalid YAML, so the registry
silently dropped the tool; todo_update failed outright with no project."""
import inspect
import re
from pathlib import Path

import pytest
import yaml

from backend.agent.tools import argcheck

ROOT = Path(__file__).resolve().parent.parent
TOOL_MDS = sorted((ROOT / "tools").glob("*/TOOL.md")) + sorted((ROOT / "skills").glob("*/SKILL.md"))


def _front(md: Path) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", md.read_text(), re.S)
    assert m, f"{md}: no front matter"
    return yaml.safe_load(m.group(1)) or {}


@pytest.mark.parametrize("md", TOOL_MDS, ids=lambda p: p.parent.name)
def test_front_matter_parses(md):
    meta = _front(md)
    assert meta.get("name") and meta.get("description"), md


@pytest.mark.parametrize("md", [m for m in TOOL_MDS if (m.parent / "handler.py").exists()
                                and m.name == "TOOL.md"], ids=lambda p: p.parent.name)
def test_schema_matches_handler(md):
    props = set(((_front(md).get("parameters") or {}).get("properties") or {}))
    src = (md.parent / "handler.py").read_text()
    m = re.search(r"async def run\((.*?)\)\s*(->[^:]*)?:", src, re.S)
    if not m or "**" in m.group(1):
        return
    parts = [a.strip() for a in m.group(1).split(",") if a.strip()]
    names = {a.split(":")[0].split("=")[0].strip() for a in parts}
    required = {a.split(":")[0].strip() for a in parts if "=" not in a}
    assert props <= names, f"TOOL.md offers {sorted(props - names)} the handler refuses"
    assert required <= props, f"handler requires {sorted(required - props)} the schema never offers"


async def _h(tab: int, max_chars: int | None = None):
    return "ok"


def test_read_only_tool_runs_without_the_unknown_argument():
    args, note, err = argcheck.prepare("t", _h, {"tab": 1, "max_char": 5}, read_only=True)
    assert err is None and args == {"tab": 1}
    assert "ignored 'max_char'" in note and "did you mean 'max_chars'" in note


def test_mutating_tool_refuses_with_the_fix():
    _, _, err = argcheck.prepare("t", _h, {"tab": 1, "max_char": 5}, read_only=False)
    assert err.startswith("error:") and "did you mean 'max_chars'" in err and "Nothing ran" in err


def test_missing_required_names_it():
    _, _, err = argcheck.prepare("t", _h, {}, read_only=True)
    assert "needs tab" in err and "max_chars?" in err


def test_crash_message_has_no_traceback():
    try:
        raise LookupError("boom")
    except LookupError as e:
        msg = argcheck.crash_message("t", e)
    assert "Traceback" not in msg and "LookupError: boom" in msg and "report_harness_fault" in msg


async def test_todo_without_a_project_keeps_a_turn_list(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("todo_h", ROOT / "tools/todo_update/handler.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)

    async def no_project():
        raise LookupError("no project is loaded in the guest for this turn")
    monkeypatch.setattr(h, "require_project", no_project)
    monkeypatch.setattr(h, "_turn_key", lambda: "op-1")
    out = await h.run("add", text="log in")
    assert "0. [ ] log in" in out and "this turn only" in out
    assert "[x] log in" in await h.run("check", text="log in")


def test_identity_arguments_are_refused_even_read_only():
    async def fetch():
        return ""
    _, _, err = argcheck.prepare("inbox_fetch", fetch, {"conversation_id": 7}, read_only=True)
    assert err and "has no parameter 'conversation_id'" in err
