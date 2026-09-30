"""Tool sections: at most 15 tools in the prompt, the rest loaded on demand
through `tools(section=...)`, merged tools with an action argument, the old
names still working, and nothing loadable that was not granted
(backend/agent/tools/toolsections.py)."""
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest
import yaml

from backend.agent.tools import registry, toolsections
from backend.agent.tools.toolsections import META, View
from backend.db import get_db, init_db

ROOT = Path(__file__).resolve().parent.parent


def _front(md: Path) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", md.read_text(), re.S)
    return yaml.safe_load(m.group(1)) or {}


def _all_specs(monkeypatch, drop=()):
    """Every registry tool granted, as if a computer, a browser and the
    projector were all connected (the worst case). Not the /local tools:
    only a local chat gets those, and it loses the sandbox tools for them."""
    monkeypatch.setattr(registry, "_requirements_met",
                        lambda e: e.get("requires_local") is not True)
    return registry.openai_tool_specs(
        [e for e in registry.load_registry() if e["name"] not in drop])


def _local_specs(monkeypatch):
    from backend import localexec
    monkeypatch.setattr(registry, "_requirements_met", lambda e: True)
    return registry.openai_tool_specs(localexec.filter_entries(registry.load_registry()))


def _names(wire):
    return [toolsections.spec_name(s) for s in wire]


def _hist(text):
    return [{"role": "user", "content": text}]


# --- the catalog -----------------------------------------------------------------

def test_every_tool_names_a_known_section():
    for md in sorted((ROOT / "tools").glob("*/TOOL.md")):
        meta = _front(md)
        assert meta.get("section") in toolsections.SECTIONS, md.parent.name


def test_core_is_at_most_fifteen_with_the_meta_tool(monkeypatch):
    specs = _all_specs(monkeypatch)
    v = View(specs, _hist("hi"))
    assert v.active
    shown = v.wire()
    assert len(shown) <= toolsections.CORE_MAX
    assert _names(shown)[-1] == META
    for must in ("read_file", "write_file", "edit_file", "run_code", "todo_update",
                 "web_search", "ask_user", "memory"):
        assert must in _names(shown), must
    # nothing a connected computer or browser adds reaches the core
    assert not any(n.startswith(("desk", "browser")) for n in _names(shown))


def test_wire_specs_carry_no_annotations(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("click it"))
    for s in v.wire():
        assert set(s) == {"type", "function"}
        assert set(s["function"]) <= {"name", "description", "parameters"}


def test_small_toolsets_pass_through_unchanged(monkeypatch):
    """The voice local tier and narrow agents keep exactly their tools."""
    specs = [s for s in _all_specs(monkeypatch)
             if toolsections.spec_name(s) in ("music_play", "music_control",
                                              "web_search", "web_read")]
    v = View(specs, _hist("hi"))
    assert not v.active
    assert v.wire() == toolsections.wire_specs(specs)


# --- loading -----------------------------------------------------------------------

def test_meta_loads_a_section_with_its_playbook_once(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("hi"))
    assert "browser" not in _names(v.wire())
    out = v.meta_call({"section": "browser"})
    assert out.startswith("Loaded section 'browser'")
    assert "Operating a browser tab" in out            # the navigation playbook
    assert "browser" in _names(v.wire())
    again = v.meta_call({"section": "browser"})
    assert again.startswith("Already loaded") and "Operating a browser tab" not in again
    listing = v.meta_call({})
    assert "- desk:" in listing and "[desk]" in listing


def test_meta_unknown_section_suggests(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("hi"))
    out = v.meta_call({"section": "brower"})
    assert out.startswith("error:") and "did you mean 'browser'" in out


def test_merged_tool_maps_to_the_real_tool(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("hi"))
    real, args, note, err = v.resolve("browser", {"action": "click", "tab": 1,
                                                  "element": "f0:3"})
    assert (real, args, err) == ("browser_click", {"tab": 1, "element": "f0:3"}, None)
    assert "section 'browser' is now loaded" in note   # called unloaded: auto-load
    real, args, _, err = v.resolve("git", {"action": "commit", "message": "m"})
    assert (real, args, err) == ("git_commit_request", {"message": "m"}, None)
    _, _, _, err = v.resolve("desk", {"action": "clik"})
    assert err.startswith("error:") and "did you mean 'click'" in err
    assert "screenshot" in err


def test_old_names_still_work_and_load_their_section(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("hi"))
    real, args, note, err = v.resolve("desk_click", {"element": 4})
    assert (real, args, err) == ("desk_click", {"element": 4}, None)
    assert "desk" in v.loaded and "desk" in _names(v.wire())
    assert "Operating the connected computer" in note
    # already loaded: no second note
    assert v.resolve("desk_type", {"text": "x"})[2] == ""


def test_unknown_name_is_refused_with_the_right_section(monkeypatch):
    v = View(_all_specs(monkeypatch), _hist("hi"))
    _, _, _, err = v.resolve("browser_clik", {})
    assert err.startswith("error:") and "Did you mean 'browser_click', in section 'browser'" in err
    _, _, _, err = v.resolve("media", {})
    assert 'tools(section="media")' in err


def test_loading_never_reveals_what_was_not_granted(monkeypatch):
    """No computer connected: the desk tools are not in the grant, so neither
    the meta-tool nor a direct call can reach them."""
    specs = _all_specs(monkeypatch, drop={n for n in (p.parent.name for p in
                                                        (ROOT / "tools").glob("desk_*/TOOL.md"))})
    v = View(specs, _hist("click the button on my screen"))
    assert "desk" not in v.sections()
    assert v.meta_call({"section": "desk"}).startswith("error: no section 'desk'")
    _, _, _, err = v.resolve("desk_click", {"element": 1})
    assert err.startswith("error: there is no tool named 'desk_click'")
    _, _, _, err = v.resolve("desk", {"action": "click"})
    assert err.startswith("error:")


def test_triggers_and_autoload(monkeypatch):
    specs = _all_specs(monkeypatch)
    v = View(specs, _hist("click the login button on my screen"))
    assert {"desk", "browser"} <= v.loaded
    assert "Operating the connected computer" in v.start_guides()
    v = View(specs, _hist("commit this and open a pull request"))
    assert "git" in v.loaded and "desk" not in v.loaded
    v = View(specs, _hist("run spawn_temp_agent on it"))          # a tool named outright
    assert "agents" in v.loaded


def test_local_chat_autoloads_its_tools_within_the_budget(monkeypatch):
    v = View(_local_specs(monkeypatch), _hist("hi"))
    shown = _names(v.wire())
    assert "local" in v.loaded and "local_shell" in shown and "read_file" not in shown
    assert len(shown) <= toolsections.CORE_MAX


def test_host_preload_marks(monkeypatch):
    specs = toolsections.mark_load(_all_specs(monkeypatch), {"media"})
    v = View(specs, _hist("hi"))
    assert "media" in v.loaded and "music_play" in _names(v.wire())


def test_wire_order_is_stable_whatever_the_load_order(monkeypatch):
    specs = _all_specs(monkeypatch)
    a = View(specs, _hist("hi"))
    a.meta_call({"section": "git"})
    a.meta_call({"section": "media"})
    b = View(specs, _hist("hi"))
    b.meta_call({"section": "media,git"})
    assert a.wire() == b.wire()


# --- toolFilters: plans, subagents, presets -------------------------------------------

def test_plan_item_gets_plan_report_shown(monkeypatch):
    from backend import agents_run
    monkeypatch.setattr(registry, "_requirements_met", lambda e: True)
    specs = agents_run._agent_tools({"name": "t"}) + agents_run._internal_specs(("plan_report",))
    v = View(specs, _hist("do the item"))
    assert "plan_report" in _names(v.wire())


def test_subagent_tools_are_sectioned_and_non_delegable_stays_out(monkeypatch):
    from backend import agents_run, autonomy
    monkeypatch.setattr(registry, "_requirements_met",
                        lambda e: e.get("requires_local") is not True)
    specs = agents_run._agent_tools({"name": "t"})
    names = {toolsections.spec_name(s) for s in specs}
    assert not (names & (autonomy.NON_DELEGABLE - {"spawn_agent", "spawn_temp_agent"}))
    v = View(specs, _hist("go"))
    assert len(v.wire()) <= toolsections.CORE_MAX
    # the agents section offers only what the subagent was granted
    assert "deploy_agents" not in v.meta_call({"section": "agents"})


def test_preset_exclusions_accept_merged_and_section_names(monkeypatch):
    from backend import agents_run
    ex = agents_run.agent_exclusions({"tools_exclude": ["browser", "music_play"]})
    assert {"browser_click", "browser_read_page", "music_play"} <= ex
    assert "music_search" not in ex
    ex = agents_run.agent_exclusions({"tools_exclude": ["desk_shell"]})
    assert ex == {"desk_shell"}                        # an old name means just that tool
    monkeypatch.setattr(registry, "_requirements_met", lambda e: True)
    specs = agents_run._agent_tools({"name": "t", "tools_exclude": ["desk_shell"]})
    v = View(specs, _hist("hi"))
    assert "shell" not in v.groups["desk"]


# --- the loop --------------------------------------------------------------------------

class _Model:
    def __init__(self, rounds):
        self.rounds, self.call, self.tools_seen = rounds, 0, []

    async def complete(self, messages, tools=None, **kw):
        self.tools_seen.append(_names(tools or []))
        self.system = messages[0]["content"]
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a)}}
                     for j, (n, a) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [], "usage": None}


async def _loop(monkeypatch, rounds, text="hi"):
    from backend.agent import loop as loop_mod
    specs = _all_specs(monkeypatch)
    model = _Model(rounds)
    dispatched = []

    async def dispatch(name, args):
        dispatched.append((name, args))
        return f"ok {name}"
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    events = [ev async for ev in loop_mod.run_turn(
        1, "SYSTEM", _hist(text), tools=specs, self_check=False)]
    return model, dispatched, events


async def test_loop_meta_call_loads_for_the_next_round(monkeypatch):
    model, dispatched, events = await _loop(monkeypatch, [
        [(META, {"section": "git"})],
        [("git", {"action": "status"})]])
    assert "git" not in model.tools_seen[0] and META in model.tools_seen[0]
    assert len(model.tools_seen[0]) <= toolsections.CORE_MAX
    assert "git" in model.tools_seen[1]
    assert dispatched == [("git_status", {})]           # never "tools", never "git"
    tools_ev = [e for e in events if e["type"] == "tool"]
    assert [e["name"] for e in tools_ev] == [META, "git_status"]


async def test_loop_unloaded_call_runs_and_loads(monkeypatch):
    model, dispatched, events = await _loop(monkeypatch, [
        [("browser_read_page", {"tab": 1})]])
    assert dispatched == [("browser_read_page", {"tab": 1})]
    res = next(e for e in events if e["type"] == "tool_result")
    assert res["ok"] and "section 'browser' is now loaded" in res["result"]
    assert "browser" in model.tools_seen[1]


async def test_loop_refuses_a_name_outside_the_grant(monkeypatch):
    model, dispatched, events = await _loop(monkeypatch, [[("inbox_fetch", {})]])
    assert dispatched == []
    res = next(e for e in events if e["type"] == "tool_result")
    assert not res["ok"] and "no tool named 'inbox_fetch'" in res["result"]


async def test_loop_puts_the_playbook_in_the_prompt_when_preloaded(monkeypatch):
    model, _, _ = await _loop(monkeypatch, [], text="click the OK button on my screen")
    assert "Operating the connected computer" in model.system
    model, _, _ = await _loop(monkeypatch, [], text="hi")
    assert "Operating the connected computer" not in model.system


# --- host preload from the conversation ---------------------------------------------------

async def test_preload_sections_from_this_conversations_calls(tmp_env, monkeypatch):
    from backend import chat, gitea
    await init_db()
    monkeypatch.setattr(gitea, "enabled", lambda: False)
    specs = _all_specs(monkeypatch)
    db = await get_db()
    try:
        cid = (await db.execute("INSERT INTO conversations (summary) VALUES ('t')")).lastrowid
        for tool, args in (("music_play", {"query": "x"}), (META, {"section": "projector"}),
                           ("read_file", {"path": "a"})):
            await db.execute("INSERT INTO tool_calls (conversation_id, tool, args, result) "
                             "VALUES (?, ?, ?, 'ok')", (cid, tool, json.dumps(args)))
        await db.commit()
        secs = await chat._preload_sections(db, cid, specs, None)
        assert {"media", "projector", "files"} <= secs and "git" not in secs
        monkeypatch.setattr(gitea, "enabled", lambda: True)
        assert "git" in await chat._preload_sections(db, cid, specs, "demo")
    finally:
        await db.close()


# --- guest parity ------------------------------------------------------------------------------

def test_guest_package_exposes_tools_the_same_way(monkeypatch):
    """The guest runs loop.py with the package's toolsections + navplaybook,
    stdlib only; its view of the same grant must equal the host's."""
    from backend.vm.guest_pkg import build_package_tar
    specs = _all_specs(monkeypatch)
    hist = _hist("click the button on my screen")
    host = View(specs, hist)
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(d, filter="data")
    with open(os.path.join(d, "in.json"), "w") as f:
        json.dump({"specs": specs, "hist": hist}, f)
    script = (
        "import json, socket\n"
        "socket.VMADDR_CID_HOST = getattr(socket, 'VMADDR_CID_HOST', 2)\n"
        "socket.AF_VSOCK = getattr(socket, 'AF_VSOCK', 40)\n"
        "from backend.agent.loop import run_turn\n"
        "from backend.agent.tools import toolsections\n"
        "x = json.load(open('in.json'))\n"
        "v = toolsections.View(x['specs'], x['hist'])\n"
        "print(json.dumps({'wire': v.wire(), 'guides': v.start_guides(),\n"
        "                  'r': v.resolve('browser', {'action': 'read', 'tab': 2})}))\n")
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=d,
                       env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    g = json.loads(r.stdout.strip().splitlines()[-1])
    assert g["wire"] == host.wire()
    assert g["guides"] == host.start_guides() != ""
    assert g["r"][:2] == ["browser_read_page", {"tab": 2}]


@pytest.mark.parametrize("module", ["backend/agent/tools/toolsections.py",
                                    "backend/navplaybook.py"])
def test_shared_modules_are_in_the_guest_package(module):
    from backend.vm import guest_pkg
    assert module in guest_pkg._COPY_MODULES
