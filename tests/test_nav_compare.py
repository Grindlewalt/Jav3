"""scripts/nav_compare.py: task validation, the safety watchdog, scoring and
the switch verdict, and a whole session against a fake Jav3 server (httpx
MockTransport, like tests/test_cli.py) — nothing touches a real desktop."""
import importlib.util
import json
import math
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("nav_compare", ROOT / "scripts/nav_compare.py")
nc = importlib.util.module_from_spec(_spec)
sys.modules["nav_compare"] = nc   # dataclasses resolve their module by name
_spec.loader.exec_module(nc)


def _task(**kw):
    t = {"id": "t-one", "prompt": "do it", "check": ["true"]}
    t.update(kw)
    return t


def _spec_dict(tasks, **kw):
    s = {"allowed_apps": ["TextEdit", "Finder", "Calculator", "Safari"],
         "scratch": "~/jav3-nav-scratch", "tasks": tasks,
         "agents": {"navigator": {"model": ""},
                    "navigator-qwen": {"model": "openrouter/qwen/qwen3.8-27b"}}}
    s.update(kw)
    return s


# --- task file -----------------------------------------------------------------

def test_shipped_task_file_is_valid():
    spec = nc.load_tasks(ROOT / "scripts/nav_tasks.yaml")
    ids = [t["id"] for t in spec["tasks"]]
    assert len(ids) == 20 == len(set(ids))
    assert all(t["max_steps"] == 30 for t in spec["tasks"])
    tags = {g for t in spec["tasks"] for g in t["tags"]}
    assert {"duplicate-trap", "changed", "scroll", "zoom", "menu-keyboard", "drag"} <= tags
    assert sum("changed" in t["tags"] for t in spec["tasks"]) >= 2
    assert spec["agents"]["navigator"]["model"] == ""
    assert spec["agents"]["navigator-qwen"]["model"] == "openrouter/qwen/qwen3.8-27b"


@pytest.mark.parametrize("bad, msg", [
    ([_task(check=[])], "missing check"),
    ([_task(), _task()], "duplicate"),
    ([_task(id="Bad Id")], "task id"),
    ([_task(setup=["sudo true"])], "forbidden"),
    ([_task(setup=["rm -rf ~/x"])], "forbidden"),
    ([_task(setup=["open -a Terminal"])], "not in allowed_apps"),
    ([_task(check=["osascript -e 'tell application \"Mail\" to get name'"])], "forbidden"),
    ([_task(cleanup=["osascript -e 'tell application \"Notes\" to quit'"])],
     "not in allowed_apps"),
    ([_task(max_steps=500)], "max_steps"),
])
def test_task_validation_refuses(bad, msg):
    with pytest.raises(nc.TaskError, match=msg):
        nc.validate_spec(_spec_dict(bad))


def test_task_validation_allows_system_events_reads_and_needs_the_scratch_dir():
    nc.validate_spec(_spec_dict([_task(check=[
        "osascript -e 'tell application \"System Events\" to get name of first process'"])]))
    with pytest.raises(nc.TaskError, match="scratch"):
        nc.validate_spec(_spec_dict([_task()], scratch="~/Documents"))


def test_prompt_placeholders_expand():
    spec = nc.validate_spec(_spec_dict([_task(prompt="save in {TASK_DIR} via {BASE_URL}")]))
    env = nc.task_env(spec, spec["tasks"][0])
    out = nc.render_prompt(spec["tasks"][0], env)
    assert out.endswith("jav3-nav-scratch/t-one via http://127.0.0.1:8765")


# --- preset --------------------------------------------------------------------

def test_preset_keeps_only_navigation_tools():
    names = ["desk_click", "desk_shell", "desk_screenshot", "browser_click",
             "web_read", "web_search", "run_code", "spawn_agent", "write_file"]
    body = nc.preset_body("navigator-qwen", {"model": "openrouter/qwen/qwen3.8-27b"}, names)
    kept = set(names) - set(body["tools_exclude"])
    assert kept == {"desk_click", "desk_screenshot", "browser_click", "web_read"}
    assert body["model"] == "openrouter/qwen/qwen3.8-27b"
    assert '"done"' in body["prompt"] and "# Objective" in body["prompt"]
    assert nc.preset_body("navigator", {"model": ""}, names)["model"] == ""


def test_parse_claim_takes_the_last_json_line():
    txt = 'I clicked it.\n{"done": true, "evidence": "title applied", "steps": 3}'
    assert nc.parse_claim(txt) == {"done": True, "evidence": "title applied", "steps": 3}
    assert nc.parse_claim("no json here") is None


# --- watchdog ------------------------------------------------------------------

SHOT = ("screen 1280x800 of \"Built-in\"\nelements (click by id; coordinates are "
        "pixels of this image):\n  [1] button \"Save\" @ 640,410 80x28\n"
        "  [2] textfield \"Name\" @ 200,60 300x24 focused\n")


def _wd(owner=None, front="TextEdit"):
    seen = []

    def owner_at(x, y, frame):
        seen.append((round(x), round(y)))
        return owner(x, y) if owner else "TextEdit"
    return nc.Watchdog(["TextEdit", "Finder", "Calculator", "Safari"],
                       "http://127.0.0.1:8765/", owner_at=owner_at,
                       front_app=lambda: front), seen


def test_watchdog_aborts_on_desk_shell():
    wd, _ = _wd()
    with pytest.raises(nc.Abort, match="desk_shell"):
        wd.on_tool("desk_shell", {"cmd": "ls"})


def test_watchdog_desk_open_scope():
    wd, _ = _wd()
    wd.on_tool("desk_open", {"app": "TextEdit"})
    wd.on_tool("desk_open", {"url": "http://127.0.0.1:8765/navfx-panel.html"})
    with pytest.raises(nc.Abort, match="app"):
        wd.on_tool("desk_open", {"app": "Terminal"})
    with pytest.raises(nc.Abort, match="url"):
        wd.on_tool("desk_open", {"url": "https://example.com/"})


def test_watchdog_resolves_element_clicks_to_the_owning_window():
    wd, seen = _wd(owner=lambda x, y: "TextEdit" if y > 100 else "Terminal")
    wd.on_result("desk_screenshot", SHOT)
    wd.on_tool("desk_click", {"element": 1})          # 640,410 -> TextEdit: fine
    assert seen == [(640, 410)]
    with pytest.raises(nc.Abort, match="Terminal"):
        wd.on_tool("desk_click", {"element": 2})      # 200,60 -> Terminal


def test_watchdog_maps_zoomed_coordinates_to_the_full_frame():
    wd, seen = _wd()
    wd.on_result("desk_screenshot", SHOT)
    wd.on_result("desk_screenshot", "zoomed region 400,300 320x200 of \"Built-in\", "
                 "shown at 1280x800\nelements (click by id):\n  [1] button \"x\" @ 640,400 16x16\n")
    wd.on_tool("desk_click", {"x": 640, "y": 400})
    assert seen[-1] == (560, 400)                     # 400 + 640*320/1280, 300 + 400*200/800
    wd.on_tool("desk_click", {"element": 1})
    assert seen[-1] == (560, 400)


def test_watchdog_keyboard_checks_the_front_app_and_target_clicks_after_the_fact():
    wd, _ = _wd(front="Messages")
    with pytest.raises(nc.Abort, match="Messages"):
        wd.on_tool("desk_type", {"text": "hi"})
    wd, _ = _wd(owner=lambda x, y: "Mail")
    wd.frame.full_w = 1280
    with pytest.raises(nc.Abort, match="Mail"):
        wd.on_result("desk_click", 'clicked "Send button" at 640,410 (grounded by m)\n' + SHOT)


# --- scoring and verdict ---------------------------------------------------------

def _runs(agent, pattern, cost=0.01, status="ok"):
    """pattern: {task: [bool per run]}"""
    return [{"agent": agent, "task": t, "run": i, "success": ok, "status": status,
             "steps": 5, "wall_s": 10.0 + i, "cost_usd": cost}
            for t, oks in pattern.items() for i, ok in enumerate(oks)]


GOOD_PROBE = {"model": "openrouter/qwen/qwen3.8-27b", "hit_rate": 0.97, "p95_ms": 1800}


def test_verdict_switches_only_when_every_criterion_passes():
    tasks = [f"t{i}" for i in range(10)]
    base = _runs("b", {t: [i < 5, i < 5, i < 5] for i, t in enumerate(tasks)})   # 50%
    cand = _runs("c", {t: [i < 8, i < 8, i < 8] for i, t in enumerate(tasks)},
                 cost=0.015)                                                      # 80%
    v = nc.verdict(base, cand, GOOD_PROBE)
    assert v["switch"], v["checks"]
    assert v["checks"]["wins"]["detail"].startswith("candidate wins 3, baseline wins 0")


def test_verdict_plus_15_points_rule():
    tasks = [f"t{i}" for i in range(10)]
    base = _runs("b", {t: [i < 5] * 3 for i, t in enumerate(tasks)})
    cand = _runs("c", {t: [i < 6] * 3 for i, t in enumerate(tasks)})             # +10
    v = nc.verdict(base, cand, GOOD_PROBE)
    assert not v["switch"] and not v["checks"]["points"]["pass"]


def test_verdict_three_to_one_rule():
    tasks = [f"t{i}" for i in range(20)]
    # candidate +8 tasks, baseline +3 tasks: +25 points but 8:3 < 3:1
    base = _runs("b", {t: [i < 8 or i in (18, 19, 17)] * 3 for i, t in enumerate(tasks)})
    cand = _runs("c", {t: [i < 16] * 3 for i, t in enumerate(tasks)})
    v = nc.verdict(base, cand, GOOD_PROBE)
    assert v["checks"]["points"]["pass"]
    assert not v["checks"]["wins"]["pass"] and not v["switch"]
    # 9:3 is exactly 3:1 and passes
    cand = _runs("c", {t: [i < 17] * 3 for i, t in enumerate(tasks)})
    assert nc.verdict(base, cand, GOOD_PROBE)["checks"]["wins"]["pass"]


def test_verdict_cost_ratio_and_gate():
    base = _runs("b", {"a": [True, False, False], "b": [False] * 3})
    cand = _runs("c", {"a": [True] * 3, "b": [True] * 3}, cost=0.05)
    # base $/success = 0.06/1; cand = 0.30/6 = 0.05 -> ok
    assert nc.verdict(base, cand, GOOD_PROBE)["checks"]["cost"]["pass"]
    cand = _runs("c", {"a": [True] * 3, "b": [True] * 3}, cost=0.13)   # 0.13 > 0.12
    assert not nc.verdict(base, cand, GOOD_PROBE)["checks"]["cost"]["pass"]
    unpriced = _runs("c", {"a": [True] * 3, "b": [True] * 3}, cost=0.0)
    assert not nc.verdict(base, unpriced, GOOD_PROBE)["checks"]["cost"]["pass"]
    cand = _runs("c", {"a": [True] * 3, "b": [True] * 3})
    assert not nc.verdict(base, cand, None)["checks"]["gate"]["pass"]
    slow = {**GOOD_PROBE, "p95_ms": 2600}
    assert not nc.verdict(base, cand, slow)["checks"]["gate"]["pass"]
    low = {**GOOD_PROBE, "hit_rate": 0.95}
    assert not nc.verdict(base, cand, low)["checks"]["gate"]["pass"]


def test_summary_excludes_infra_failures_and_reports_percentiles():
    runs = _runs("b", {"a": [True, False, True]}) + _runs("b", {"z": [False]}, status="infra_error")
    s = nc.summarize(runs)
    assert s["runs"] == 3 and s["excluded"] == 1 and s["successes"] == 2
    assert s["p50_wall"] == 11.0 and s["p95_wall"] == 12.0
    assert math.isclose(s["cost_per_success"], 0.015)
    assert math.isinf(nc.summarize(_runs("b", {"a": [False]}))["cost_per_success"])


# --- whole sessions against a fake server ---------------------------------------

def _write_tasks(tmp_path, n=2):
    spec = _spec_dict([_task(id=f"t-{i}", setup=["true"], cleanup=["true"], max_steps=4)
                       for i in range(n)],
                      scratch=str(tmp_path / "jav3-nav-scratch"),
                      fixtures={"navfx-a.html": "<title>a</title>"})
    p = tmp_path / "tasks.yaml"
    p.write_text(yaml.safe_dump(spec))
    return p


class Fake:
    def __init__(self, events_for):
        self.events_for, self.calls, self.cid = events_for, [], 100
        self.agents = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        self.calls.append((m, path))
        body = json.loads(req.content) if req.content else None
        if path == "/api/tools":
            return httpx.Response(200, json={"tools": [{"name": n} for n in (
                "desk_click", "desk_shell", "desk_screenshot", "run_code")]})
        if path.startswith("/api/agents"):
            slug = path.rsplit("/", 1)[-1]
            if m == "GET":
                return httpx.Response(200 if slug in self.agents else 404, json={})
            if m == "POST":
                self.agents[body["name"]] = {}
                return httpx.Response(200, json={"slug": body["name"]})
            self.agents[slug] = body
            return httpx.Response(200, json={"ok": True})
        if path == "/api/grounding":
            return httpx.Response(200, json={"ranking": [GOOD_PROBE]})
        if path == "/api/chat":
            self.cid += 1
            evs = [{"type": "start", "conversation_id": self.cid, "model": "m"}]
            evs += self.events_for(body)
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in evs)
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        if path.endswith("/stop"):
            return httpx.Response(200, json={"stopped": True})
        if path.endswith("/info"):
            return httpx.Response(200, json={"input_tokens": 1000, "output_tokens": 10,
                                             "cost_usd": 0.002, "model": "m"})
        return httpx.Response(404, json={})


@pytest.fixture
def no_fixture_server(monkeypatch):
    class Nop:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *e):
            pass
    monkeypatch.setattr(nc, "FixtureServer", Nop)


def _main(tmp_path, fake, extra=(), local=None):
    lines, ran = [], []

    def fake_local(cmds, env):
        ran.append((env["TASK_DIR"], list(cmds)))
        return True, ""
    code = nc.main(["--tasks", str(_write_tasks(tmp_path)), "--runs", "1",
                    "--out", str(tmp_path / "out"), "--server", "http://h", *extra],
                   client=httpx.Client(transport=httpx.MockTransport(fake)),
                   out=lines.append, owner_at=lambda x, y, f: "TextEdit",
                   front_app=lambda: "TextEdit", local=local or fake_local)
    return code, lines, ran


def test_dry_run_calls_nothing(tmp_path):
    def boom(req):
        raise AssertionError(f"dry run made a request: {req.url}")
    lines, ran = [], []
    code = nc.main(["--tasks", str(_write_tasks(tmp_path)), "--dry-run"],
                   client=httpx.Client(transport=httpx.MockTransport(boom)),
                   out=lines.append, local=lambda c, e: ran.append(c))
    assert code == 0 and not ran
    assert lines[0].startswith("12 runs: 2 agents x 2 tasks x 3 runs")
    assert any("t-1" in ln for ln in lines)
    assert not (tmp_path / "jav3-nav-scratch").exists()


def test_session_scores_runs_creates_presets_and_writes_logs(tmp_path, no_fixture_server):
    def events(body):
        return [{"type": "tool", "id": "1", "name": "desk_screenshot", "args": {}},
                {"type": "tool_result", "id": "1", "name": "desk_screenshot", "ok": True,
                 "result": SHOT},
                {"type": "tool", "id": "2", "name": "desk_click", "args": {"element": 1}},
                {"type": "tool_result", "id": "2", "name": "desk_click", "ok": True,
                 "result": "clicked [1] button \"Save\" at 640,410\nchanged: yes"},
                {"type": "final", "content": '{"done": true, "evidence": "ok", "steps": 2}'}]
    fake = Fake(events)
    code, lines, ran = _main(tmp_path, fake)
    assert code == 0
    assert set(fake.agents) == {"navigator", "navigator-qwen"}
    assert fake.agents["navigator"]["tools_exclude"] == ["desk_shell", "run_code"]
    chats = [c for c in fake.calls if c == ("POST", "/api/chat")]
    assert len(chats) == 4
    logs = sorted((tmp_path / "out").glob("*.json"))
    assert len(logs) == 4
    rec = json.loads(logs[0].read_text())
    assert rec["success"] and rec["steps"] == 2 and rec["cost_usd"] == 0.002
    assert rec["claim"]["done"] is True
    report = (tmp_path / "out/report.md").read_text()
    assert "| navigator | 2 | 100.0%" in report and "KEEP the baseline" in report
    assert (tmp_path / "jav3-nav-scratch/web/navfx-a.html").exists()


def test_session_aborts_everything_on_desk_shell(tmp_path, no_fixture_server, monkeypatch):
    stopped = []
    monkeypatch.setattr(nc, "stop_desk", lambda pid_file: stopped.append(1) or "stopped")

    def events(body):
        return [{"type": "tool", "id": "1", "name": "desk_shell", "args": {"cmd": "ls"}},
                {"type": "tool_result", "id": "1", "name": "desk_shell", "ok": True,
                 "result": "x"},
                {"type": "final", "content": "done"}]
    fake = Fake(events)
    code, lines, ran = _main(tmp_path, fake)
    assert stopped == [1]
    assert fake.calls.count(("POST", "/api/chat")) == 1          # the session ended
    assert ("POST", "/api/chat/101/stop") in fake.calls
    assert any("WATCHDOG" in ln for ln in lines)
    rec = json.loads(next((tmp_path / "out").glob("*.json")).read_text())
    assert rec["status"] == "aborted" and not rec["success"]
    assert ran[-1][1] == ["true"]                                 # cleanup still ran


def test_session_stops_a_turn_past_max_steps(tmp_path, no_fixture_server):
    def events(body):
        out = []
        for i in range(8):
            out += [{"type": "tool", "id": str(i), "name": "desk_screenshot", "args": {}},
                    {"type": "tool_result", "id": str(i), "name": "desk_screenshot",
                     "ok": True, "result": SHOT}]
        return out + [{"type": "final", "content": "done"}]
    fake = Fake(events)
    code, lines, ran = _main(tmp_path, fake, extra=("--agents", "navigator"))
    recs = [json.loads(p.read_text()) for p in (tmp_path / "out").glob("*.json")]
    assert all(r["status"] == "max_steps" and r["steps"] == 5 and not r["success"]
               for r in recs)
    assert ("POST", "/api/chat/101/stop") in fake.calls
