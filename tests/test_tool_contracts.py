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


# --- RUNS-16: the misspelled key is named, and an unambiguous one is remapped ---

async def _edit(path: str, find: str, replace: str, all: bool = False):
    return "ok"


def test_missing_argument_error_names_the_misspelled_key():
    # conversation 553: four edit_file calls passed 'replacement' for 'replace'
    # two keys claim the same argument, so neither is guessed: both are named
    _, _, err = argcheck.prepare("edit_file", _edit,
                                 {"path": "a", "find": "b", "replacemen": "c",
                                  "replacement": "d"}, read_only=False)
    assert "needs replace" in err
    assert "'replacement'" in err and "'replacemen'" in err and "did you mean 'replace'" in err


def test_unique_close_key_for_the_missing_required_one_is_remapped():
    args, note, err = argcheck.prepare("edit_file", _edit,
                                       {"path": "a", "find": "b", "replacement": "c"},
                                       read_only=False)
    assert err is None and args == {"path": "a", "find": "b", "replace": "c"}
    assert "took 'replacement' as 'replace'" in note


def test_a_distant_key_is_named_but_not_remapped():
    _, _, err = argcheck.prepare("edit_file", _edit, {"path": "a", "find": "b", "txt": "c"},
                                 read_only=False)
    assert err and "'txt'" in err and "needs replace" in err


def test_remap_never_overwrites_a_key_the_model_did_send():
    args, _, err = argcheck.prepare("edit_file", _edit,
                                    {"path": "a", "find": "b", "replace": "c", "replacement": "d"},
                                    read_only=False)
    assert err and "'replacement'" in err     # ambiguous: refuse instead of picking one


# --- argument TYPES (TOOLS-03/04/10): checked from the handler's annotations ---

async def _typed(text: str, n: int = 3, flag: bool = False, rate: float | None = None,
                 items: list | None = None, opts: dict | None = None,
                 either: str | list | None = None, free=None):
    return "ok"


def _prep(**args):
    return argcheck.prepare("t", _typed, {"text": "x", **args}, read_only=False)


@pytest.mark.parametrize("given,want", [("0", 0), (" 12 ", 12), (7, 7), (4.0, 4), ("-2", -2)])
def test_whole_numbers_come_from_digit_strings_and_whole_floats(given, want):
    args, _, err = _prep(n=given)
    assert err is None and args["n"] == want and type(args["n"]) is int


@pytest.mark.parametrize("given,want", [("true", True), ("False", False), ("yes", True),
                                        ("no", False), ("1", True), (0, False), (True, True)])
def test_booleans_come_from_plain_words(given, want):
    args, _, err = _prep(flag=given)
    assert err is None and args["flag"] is want


def test_other_shapes_are_coerced_only_when_the_meaning_is_plain():
    args, _, err = _prep(rate="0.5", items='["a", "b"]', opts='{"k": 1}', text=12)
    assert err is None
    assert args["rate"] == 0.5 and args["items"] == ["a", "b"] and args["opts"] == {"k": 1}
    assert args["text"] == "12"
    assert _prep(either=["a"])[2] is None and _prep(either="a")[2] is None


@pytest.mark.parametrize("bad,phrase", [
    ({"n": "four"}, "n must be a whole number (got 'four')"),
    ({"n": 1.5}, "n must be a whole number (got 1.5)"),
    ({"n": True}, "n must be a whole number (got true)"),
    ({"n": ["1"]}, "n must be a whole number (got a list)"),
    ({"flag": "maybe"}, "flag must be true or false (got 'maybe')"),
    ({"flag": 2}, "flag must be true or false (got 2)"),
    ({"text": {"k": 1}}, "text must be text (got an object)"),
    ({"text": True}, "text must be text (got true)"),
    ({"text": None}, "text is required (got null)"),
    ({"items": "a.py"}, "items must be a list (got 'a.py')"),
    ({"opts": [1]}, "opts must be an object (got a list)"),
    ({"rate": "fast"}, "rate must be a number (got 'fast')"),
    ({"either": {"k": 1}}, "either must be text or a list (got an object)"),
])
def test_uncoercible_types_are_one_plain_error_that_is_not_a_harness_fault(bad, phrase):
    _, _, err = _prep(**bad)
    assert err and phrase in err and "Nothing ran" in err
    assert "harness fault" not in err and "report_harness_fault" not in err


def test_null_for_an_optional_argument_means_not_given():
    args, _, err = _prep(n=None, flag=None, rate=None, items=None)
    assert err is None
    assert "n" not in args and "flag" not in args      # the defaults apply
    assert args["rate"] is None and args["items"] is None   # None was allowed anyway


def test_every_bad_argument_is_reported_at_once():
    _, _, err = _prep(n="x", flag="y")
    assert "n must be" in err and "flag must be" in err


def test_unannotated_parameters_are_left_to_the_handler():
    args, _, err = _prep(free={"anything": [1]})
    assert err is None and args["free"] == {"anything": [1]}


def _load_handler(tool: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"contract_{tool}",
                                                  ROOT / "tools" / tool / "handler.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.run


HANDLERS = sorted(p.parent.name for p in (ROOT / "tools").glob("*/handler.py"))


@pytest.mark.parametrize("tool", HANDLERS)
def test_a_wrong_typed_argument_never_reaches_any_handler(tool):
    """Every annotated parameter of every tool refuses an object for a scalar
    (and a scalar for an object) before the handler runs, so none can crash on it."""
    run = _load_handler(tool)
    for p in inspect.signature(run).parameters.values():
        parsed = argcheck._kinds(p.annotation)
        if parsed is None:
            continue
        kinds, _ = parsed
        bad = [{"k": 1}] if dict not in kinds else [3.5j]      # 3.5j: nothing coerces a complex
        for value in bad:
            _, _, err = argcheck.prepare(tool, run, {p.name: value}, read_only=False)
            missing_first = err and err.startswith(f"error: {tool} needs")
            assert err and (missing_first or f"{p.name} must be" in err), (tool, p.name, err)


@pytest.fixture
def proj(tmp_env):
    d = tmp_env / "projects" / "p"
    d.mkdir(parents=True)
    (d / "project.md").write_text("# p\n")
    (d / "README.md").write_text("hello\nworld\na(b\n")
    return d


async def _tool(name: str, **args) -> str:
    from backend import runtime
    from backend.agent.tools import registry
    runtime.active_project.set("p")
    return await registry.dispatch(name, args)


async def test_write_file_with_structured_content_is_a_fixable_error(proj):
    for bad in ({"k": 1}, ["a"], None, True):
        out = await _tool("write_file", path="x.json", content=bad)
        assert out.startswith("error: write_file: content ") and "harness fault" not in out, out
    assert not (proj / "x.json").exists()
    assert (await _tool("write_file", path="n.txt", content=42)).startswith("wrote n.txt")
    assert (proj / "n.txt").read_text() == "42"


async def test_dashboard_with_bad_types_is_a_fixable_error(proj):
    for kw in ({"path": "d.html", "html": None}, {"path": 5, "html": "<p>"},
               {"path": None, "html": "<p>"}):
        out = await _tool("dashboard", **kw)
        assert out.startswith("error:") and "harness fault" not in out, out


async def test_todo_update_takes_the_index_as_a_digit_string(proj):
    await _tool("todo_update", action="add", text="first")
    out = await _tool("todo_update", action="check", index="0")
    assert "0. [x] first" in out, out
    out = await _tool("todo_update", action="check", index="two")
    assert out.startswith("error: todo_update: index must be a whole number (got 'two')"), out


async def test_research_angles_as_words_is_a_fixable_error(proj):
    out = await _tool("research", topic="tides", angles="four")
    assert out.startswith("error: research: angles must be a whole number (got 'four')"), out


async def test_search_codebase_none_query_and_bool_flag(proj):
    out = await _tool("search_codebase", query=None)
    assert out.startswith("error: search_codebase: query is required"), out
    # regex='false' used to mean regex ON (a non-empty string is truthy): 'a(' is not a pattern
    out = await _tool("search_codebase", query="a(", regex="false")
    assert "a(b" in out and not out.startswith("error"), out
    assert (await _tool("search_codebase", query="a(", regex="yes")).startswith("error: bad regex")


# --- TOOLS-05: empty and impossible paths are the model's mistake, said plainly ---

@pytest.mark.parametrize("path", ["", "  ", ".", "./", "sub/.."])
async def test_write_file_with_no_real_path_says_path_is_required(proj, path):
    out = await _tool("write_file", path=path, content="x")
    assert out.startswith("error: path is required"), out
    assert "harness fault" not in out


async def test_write_file_under_an_existing_file_is_a_path_error(proj):
    (proj / "notes.txt").write_text("a")
    out = await _tool("write_file", path="notes.txt/inner.txt", content="x")
    assert out.startswith("error: write_file:") and "really a file" in out, out
    assert "harness fault" not in out


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../b"])
async def test_paths_that_leave_the_project_read_as_the_models_path(proj, path):
    for tool, args in (("write_file", {"content": "x"}), ("read_file", {}),
                       ("edit_file", {"find": "a", "replace": "b"})):
        out = await _tool(tool, path=path, **args)
        assert out.startswith(f"error: {tool}:") and "outside the project" in out, out
        assert "harness fault" not in out


async def test_a_nul_in_a_path_is_a_path_error(proj):
    out = await _tool("read_file", path="a\x00b")
    assert out.startswith("error: read_file:") and "NUL" in out, out


async def test_protected_paths_are_refused_plainly(proj):
    out = await _tool("write_file", path=".git/config", content="x")
    assert out.startswith("error: write refused — cannot write into .git"), out
    (proj / ".git").mkdir()
    (proj / ".git" / "config").write_text("[core]\n")
    out = await _tool("edit_file", path=".git/config", find="core", replace="x")
    assert out.startswith("error: edit refused — cannot write into .git"), out


async def test_edit_file_with_an_empty_find_changes_nothing(proj):
    before = (proj / "README.md").read_text()
    for kw in ({}, {"all": True}):
        out = await _tool("edit_file", path="README.md", find="", replace="X", **kw)
        assert out.startswith("error: 'find' is empty"), out
    assert (proj / "README.md").read_text() == before


async def test_edit_file_on_a_binary_file_is_a_plain_error(proj):
    (proj / "b.bin").write_bytes(b"\xff\xfe\x00\x80")
    out = await _tool("edit_file", path="b.bin", find="a", replace="b")
    assert out.startswith("error: b.bin is binary"), out


# --- TOOLS-06: a TOOL.md body reaches the model whole, or visibly cut ---

def _entry(md: Path) -> dict:
    """The registry entry for a TOOL.md, with the gates (desk connected, a
    browser attached, settings) removed so the spec is built."""
    from backend.agent.tools import registry
    e = registry._parse_md(md)
    for k in [k for k in e if k.startswith("requires_")]:
        del e[k]
    e["enabled"] = True
    return e


TOOL_ONLY = [m for m in TOOL_MDS if m.name == "TOOL.md"]


@pytest.mark.parametrize("md", TOOL_ONLY, ids=lambda p: p.parent.name)
def test_tool_body_fits_the_spec_cap(md):
    """The tail of a long body is where the failure-recovery guidance lives; past
    the cap it was cut mid-sentence and the model never saw it. Shorten the body
    (lead with what matters) or raise SPEC_NOTES_MAX deliberately."""
    from backend.agent.tools.registry import SPEC_NOTES_MAX
    body = _entry(md)["body"]
    assert len(body) <= SPEC_NOTES_MAX, (
        f"{md.parent.name}: body is {len(body)} chars, the spec carries {SPEC_NOTES_MAX}")


@pytest.mark.parametrize("md", TOOL_ONLY, ids=lambda p: p.parent.name)
def test_the_spec_carries_the_whole_body(md):
    from backend.agent.tools import registry
    e = _entry(md)
    if not e["body"]:
        return
    (spec,) = registry.openai_tool_specs([e])
    assert spec["function"]["description"].endswith(e["body"])


def test_a_body_over_the_cap_is_cut_with_a_marker():
    from backend.agent.tools import registry
    e = {"name": "t", "kind": "tool", "description": "d", "parameters": {},
         "body": "x" * (registry.SPEC_NOTES_MAX + 500)}
    (spec,) = registry.openai_tool_specs([e])
    notes = spec["function"]["description"].partition("\nNotes: ")[2]
    assert notes == "x" * registry.SPEC_NOTES_MAX + "…"
    (spec,) = registry.openai_tool_specs([e], notes_max=50)      # the local voice tier
    assert spec["function"]["description"].endswith("x" * 50 + "…")
