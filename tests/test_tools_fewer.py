"""Fewer tools in front of the model (B11): a core of ten plus the `tools`
meta-tool, near-duplicates folded into action tools, at most ~35 names with
every section loaded, and every folded call still running under its real
name (backend/agent/tools/toolsections.py, tools/*/TOOL.md `action:`)."""
import json
import re
from pathlib import Path

import yaml

from backend.agent.tools import registry, toolsections
from backend.agent.tools.toolsections import META, SUB, View

ROOT = Path(__file__).resolve().parent.parent


def _front(md: Path) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", md.read_text(), re.S)
    return yaml.safe_load(m.group(1)) or {}


def _specs(monkeypatch, names=None):
    """Every registry tool granted as if everything were connected; `names`
    also grants those `enabled: false` tools (a plan run's, plan_report...)."""
    monkeypatch.setattr(registry, "_requirements_met",
                        lambda e: e.get("requires_local") is not True)
    entries = registry.load_registry()
    extra = [{**e, "enabled": True} for e in entries
             if e["name"] in (names or ()) and e.get("enabled") is False]
    return registry.openai_tool_specs(entries) + registry.openai_tool_specs(extra)


def _hist(text):
    return [{"role": "user", "content": text}]


def _names(wire):
    return [toolsections.spec_name(s) for s in wire]


PLAN = ("plan_status", "plan_fix", "plan_report")


def _loaded_view(monkeypatch, extra=PLAN):
    v = View(_specs(monkeypatch, extra), _hist("hi"))
    v.meta_call({"section": ",".join(v.sections())})
    return v


# --- how many names ---------------------------------------------------------------

def test_the_model_sees_at_most_35_names_with_every_section_loaded(monkeypatch):
    v = _loaded_view(monkeypatch)
    names = _names(v.wire())
    assert len(names) <= 35, names
    assert len(names) == len(set(names))
    # the families that used to be several tools each are one now
    for one in ("web", "media", "project", "agents", "plans", "projector", "system",
                "browser", "desk", "git", "services", "memory"):
        assert one in names, one
    for gone in ("music_play", "music_control", "spawn_agent", "send_message", "plan_fix",
                 "projector_show", "load_project", "workspace_panel", "journal_update",
                 "web_read", "self_docs", "play_movie"):
        assert gone not in names, gone


def test_the_turn_start_is_ten_tools_and_the_meta_tool(monkeypatch):
    v = View(_specs(monkeypatch), _hist("hi"))
    shown = v.wire()
    assert sorted(_names(shown)[:-1]) == sorted([
        "ask_user", "edit_file", "list_files", "memory", "read_file", "run_code",
        "search_codebase", "todo_update", "web", "write_file"])
    assert _names(shown)[-1] == META and len(shown) == 11
    # a guard on the size of what rides every call (chars/4 ~ tokens)
    assert len(json.dumps(shown)) <= 16_000


def test_a_core_flag_is_uniform_within_a_merged_tool():
    by_section: dict[str, list[dict]] = {}
    for md in sorted((ROOT / "tools").glob("*/TOOL.md")):
        meta = _front(md)
        if meta.get("action"):
            by_section.setdefault(meta["section"], []).append(meta)
    assert {"web", "media", "project", "agents", "plans", "projector", "system"} <= set(by_section)
    for sec, metas in by_section.items():
        acts = [str(m["action"]) for m in metas]
        assert len(acts) == len(set(acts)), (sec, acts)          # one label per tool
        assert len({m.get("core") is True for m in metas}) == 1, sec


# --- every folded tool stays reachable, under its real name ------------------------

def test_every_action_maps_to_its_real_tool_and_every_old_name_still_works(monkeypatch):
    specs = _specs(monkeypatch, PLAN)
    v = View(specs, _hist("hi"))
    assert v.groups
    for group, acts in v.groups.items():
        for act, real in acts.items():
            args = {"action": act}
            if real in v.sub_action:
                args[SUB] = "x"                      # any value: only the mapping matters
            got, rest, _, err = v.resolve(group, args)
            if err:                                   # a bad `do` is refused, never dispatched
                assert real in v.sub_action and "needs do" in err
                continue
            assert got == real, (group, act)
            # the old name goes straight through, unchanged
            assert v.resolve(real, {"q": 1})[:2] == (real, {"q": 1})
    # nothing was folded that was not granted
    granted = {toolsections.spec_name(s) for s in specs}
    assert {r for acts in v.groups.values() for r in acts.values()} <= granted


def test_a_tool_with_its_own_action_argument_gets_do(monkeypatch):
    v = View(_specs(monkeypatch, PLAN), _hist("hi"))
    assert v.resolve("media", {"action": "control", SUB: "pause"})[:2] == (
        "music_control", {"action": "pause"})
    assert v.resolve("projector", {"action": "universe", SUB: "focus", "target": "mars"})[:2] == (
        "projector_universe", {"action": "focus", "target": "mars"})
    # its own action is optional there: leaving `do` out leaves it out
    assert v.resolve("projector", {"action": "output", "calibrate": True})[:2] == (
        "projector_output", {"calibrate": True})
    assert v.resolve("plans", {"action": "fix", SUB: "accept", "item": "i1",
                               "summary": "ran it"})[:2] == (
        "plan_fix", {"action": "accept", "item": "i1", "summary": "ran it"})
    assert v.resolve("project", {"action": "panel", SUB: "tile"})[:2] == (
        "workspace_panel", {"action": "tile"})
    # the merged schema names it, with every value, and lists it per action
    media = next(s for s in v.wire() if toolsections.spec_name(s) == "media")
    props = media["function"]["parameters"]["properties"]
    assert props[SUB]["enum"] == ["pause", "resume", "next", "prev", "volume", "stop"]
    assert "control(do=pause|resume|next|prev|volume|stop" in media["function"]["description"]
    assert "action" in props and "control" in props["action"]["enum"]


def test_do_errors_list_the_values_and_nothing_runs(monkeypatch):
    v = View(_specs(monkeypatch), _hist("hi"))
    real, _, _, err = v.resolve("media", {"action": "control"})       # required there
    assert err.startswith("error:") and "needs do, one of: pause, resume" in err
    assert "Nothing ran" in err
    _, _, _, err = v.resolve("media", {"action": "control", SUB: "pasue"})
    assert "did you mean 'pause'" in err
    _, _, _, err = v.resolve("media", {"action": "pause"})              # a `do` value, not an action
    assert err.startswith("error: media needs action") and "did you mean" in err


def test_a_granted_set_of_fourteen_or_fewer_is_shown_exactly_as_granted(monkeypatch):
    """The voice local tier's eight tools stay eight, unmerged, unsectioned."""
    from backend.voice import LOCAL_TOOLS
    specs = _specs(monkeypatch)
    small = [s for s in specs if toolsections.spec_name(s) in LOCAL_TOOLS]
    assert len(small) == len(LOCAL_TOOLS) <= toolsections.FLAT_MAX
    v = View(small, _hist("play some music"))
    assert not v.active and not v.groups
    assert v.wire() == toolsections.wire_specs(small)
    assert "music_control" in _names(v.wire()) and "web_read" in _names(v.wire())
    # one more tool than FLAT_MAX and the same tools fold
    big = [s for s in specs if toolsections.spec_name(s) in LOCAL_TOOLS
           or toolsections.spec_name(s).startswith(("git_", "service_", "memory_"))]
    assert len(big) > toolsections.FLAT_MAX
    assert "media" in View(big, _hist("hi")).groups


# --- what loads when ------------------------------------------------------------------

def test_using_a_core_tool_never_preloads_its_section(monkeypatch):
    specs = _specs(monkeypatch, PLAN)
    assert toolsections.sections_for(
        ["todo_update", "read_file", "ask_user", "web_search", "memory_write"], specs) == set()
    # a deferred tool's section, by its real name
    assert toolsections.sections_for(["journal_update", "send_message", "plan_report"], specs) == {
        "project", "agents", "plans"}


def test_a_plan_run_brings_the_team_tools_with_it(monkeypatch):
    v = View(_specs(monkeypatch, PLAN), _hist("check on the run"))
    assert {"plans", "agents"} <= v.loaded
    shown = _names(v.wire())
    assert "plans" in shown and "agents" in shown
    real, args, _, err = v.resolve("agents", {"action": "send", "to": "item:i2", "message": "hi"})
    assert (real, args, err) == ("send_message", {"to": "item:i2", "message": "hi"}, None)
    # a plan item's brief names plan_report and send_message: both arrive
    item = View(_specs(monkeypatch, ("plan_report",)),
                _hist("report with plan_report; send_message to item:i2 for help"))
    assert {"plans", "agents"} <= item.loaded


def test_delegation_journal_and_project_words_load_their_sections(monkeypatch):
    specs = _specs(monkeypatch)
    assert "agents" in View(specs, _hist("have the recon agent do it")).loaded
    assert "project" in View(specs, _hist("add this to the journal")).loaded
    assert "project" in View(specs, _hist("switch project to demo")).loaded
    assert "media" in View(specs, _hist("pause the music")).loaded
    assert "web" not in View(specs, _hist("hi")).loaded            # core, never deferred
    # a Gitea-backed project: the host marks git
    assert "git" in View(toolsections.mark_load(specs, {"git"}), _hist("hi")).loaded


def test_asking_the_meta_tool_for_a_core_section_is_not_an_error(monkeypatch):
    v = View(_specs(monkeypatch), _hist("hi"))
    out = v.meta_call({"section": "web"})
    assert not out.startswith("error:") and "core tools" in out
    assert v.meta_call({"section": "web,media"}).count("\n") >= 1
    assert "media" in v.loaded


def test_an_unloaded_merged_tool_called_by_name_loads_and_runs(monkeypatch):
    v = View(_specs(monkeypatch), _hist("hi"))
    assert "media" not in _names(v.wire())
    real, args, note, err = v.resolve("media", {"action": "play", "query": "jazz"})
    assert (real, args, err) == ("music_play", {"query": "jazz"}, None)
    assert "section 'media' is now loaded" in note
    assert "media" in _names(v.wire())
    real, _, note, err = View(_specs(monkeypatch), _hist("hi")).resolve(
        "send_message", {"to": "x", "message": "y"})
    assert real == "send_message" and "section 'agents' is now loaded" in note


def test_the_grant_fence_holds_for_the_new_merged_tools(monkeypatch):
    """No projector configured, no computer, no plan: those tools are not
    granted, so no section, merged action or old name reaches them."""
    monkeypatch.setattr(registry, "_requirements_met", lambda e: not e.get("requires_settings")
                        and not e.get("requires_desk") and not e.get("requires_browser")
                        and e.get("requires_local") is not True)
    v = View(registry.openai_tool_specs(registry.load_registry()),
             _hist("put the universe on the projector surface"))
    assert "projector" not in v.sections() and "plans" not in v.sections()
    assert v.meta_call({"section": "projector"}).startswith("error: no section 'projector'")
    _, _, _, err = v.resolve("projector_show", {"surface": "1"})
    assert err.startswith("error: there is no tool named 'projector_show'")
    _, _, _, err = v.resolve("plan_fix", {"action": "skip"})
    assert err.startswith("error:")
    # package_request and screenshot need vm boxes: the project tool omits them
    v.meta_call({"section": "project"})
    proj = next(s for s in v.wire() if toolsections.spec_name(s) == "project")
    acts = proj["function"]["parameters"]["properties"]["action"]["enum"]
    assert "packages" not in acts and "screenshot" not in acts and "load" in acts
    assert v.resolve("project", {"action": "packages"})[3].startswith("error: project needs action")


# --- the loop: calls dispatch under the real per-action handler --------------------------

class _Model:
    def __init__(self, rounds):
        self.rounds, self.call, self.seen = rounds, 0, []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append(_names(tools or []))
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a)}}
                     for j, (n, a) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [], "usage": None}


async def test_folded_calls_reach_the_registry_under_their_real_names(monkeypatch):
    from backend.agent import loop as loop_mod
    specs = _specs(monkeypatch, PLAN)
    model = _Model([[("web", {"action": "search", "query": "x"}),
                     ("web", {"action": "read", "url": "https://example.com"})],
                    [("media", {"action": "control", "do": "pause"}),
                     ("project", {"action": "journal", "entry": "e"}),
                     ("agents", {"action": "spawn_temp", "task": "t", "prompt": "p"})],
                    [("plans", {"action": "fix", "do": "skip", "item": "i1"}),
                     ("web_search", {"query": "old name"})]])
    seen = []

    async def dispatch(name, args):
        seen.append((name, args))
        return f"ok {name}"
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    events = [ev async for ev in loop_mod.run_turn(
        1, "SYSTEM", _hist("hi"), tools=specs, self_check=False)]
    assert seen == [
        ("web_search", {"query": "x"}),
        ("web_read", {"url": "https://example.com"}),
        ("music_control", {"action": "pause"}),
        ("journal_update", {"entry": "e"}),
        ("spawn_temp_agent", {"task": "t", "prompt": "p"}),
        ("plan_fix", {"action": "skip", "item": "i1"}),
        ("web_search", {"query": "old name"})]
    # the tool events (the ledger, the UI) carry the real names too
    assert [e["name"] for e in events if e["type"] == "tool"] == [n for n, _ in seen]
    # round one: the core, the meta-tool, and the plan run's two autoloaded sections
    assert len(model.seen[0]) <= toolsections.CORE_MAX + 2 and "media" not in model.seen[0]
    # calling a merged tool loaded its section for the next round
    assert {"media", "project", "agents"} <= set(model.seen[2])
