"""Backlog B8 (the ReAct loop): RUNS-11 the budget stop is marked, RUNS-03
eviction by pressure and by what the model still needs, RUNS-08 per-turn stats.
No DB and no network: the model and the registry are scripted stand-ins."""
import json

from backend.agent import loop as loop_mod
from backend.agent.budget import BudgetExceeded
from backend.agent.tools import registry
from backend.config import settings


class ScriptedModel:
    """Rounds of (tool, args-dict) calls, then a final answer. Snapshots the
    message list each call sees (as (role, content-string))."""
    def __init__(self, rounds, last="done", ids=None):
        self.rounds, self.last, self.ids = rounds, last, ids
        self.call = 0
        self.seen: list[list[tuple[str, str]]] = []
        self.offered: list[list[str]] = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([(m["role"], m["content"] if isinstance(m["content"], str)
                           else json.dumps(m["content"])) for m in messages])
        self.offered.append([t["function"]["name"] for t in (tools or [])])
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": n, "arguments": json.dumps(a)}}
                     for j, (n, a) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": self.last, "tool_calls": [], "usage": None}


def tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


async def run(monkeypatch, model, dispatch, tools=("read_file", "edit_file"),
              read_only=("read_file",), max_iterations=None):
    monkeypatch.setattr(loop_mod, "model", model)
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset(read_only))
    loop_mod._files_seen.clear()
    events = []
    async for ev in loop_mod.run_turn(
            1, "system", [{"role": "user", "content": "go"}],
            tools=[tool(t) for t in tools], max_iterations=max_iterations,
            self_check=False):
        events.append(ev)
    return events


# --- RUNS-11 ------------------------------------------------------------------

async def test_a_budget_stop_is_marked_on_the_final_event(tmp_env, monkeypatch):
    class Spent:
        async def complete(self, messages, tools=None, **kw):
            raise BudgetExceeded("token budget spent (1 in / 2 out)")
            yield  # pragma: no cover

    async def dispatch(name, args):
        return "ok"

    events = await run(monkeypatch, Spent(), dispatch)
    final = events[-1]
    assert final["type"] == "final" and final["content"].startswith("(stopped: token budget")
    assert final["stop"] == "budget"


# --- RUNS-03: eviction by pressure, stale reads first, an outline, re-reads counted ---

BIG = 8_000


def _source(name, n=BIG):
    """A JS-looking file of about n chars with a few symbols."""
    head = f"export class {name} {{\n  update(dt) {{\n  }}\n}}\nfunction build{name}() {{}}\n"
    return head + "// filler\n" * ((n - len(head)) // 10)


def _dispatch_files(files):
    async def dispatch(name, args):
        if name == "read_file":
            return files.get(args.get("path"), "error: no such file")
        return "edited"
    return dispatch


def _stats(events):
    return next(e for e in events if e["type"] == "turn_stats")


async def test_big_reads_stay_while_the_context_is_small(tmp_env, monkeypatch):
    """216 of 446 read_file calls in 14 days were re-reads of a result dropped
    two rounds after it was read, at any context size."""
    files = {f"src/f{i}.js": _source(f"F{i}") for i in range(6)}
    rounds = [[("read_file", {"path": p})] for p in files]
    model = ScriptedModel(rounds)
    events = await run(monkeypatch, model, _dispatch_files(files))
    assert events[-1] == {"type": "final", "content": "done"}
    assert not any("dropped to keep" in c for _r, c in model.seen[-1])
    assert _stats(events)["evictions"] == 0


async def test_pressure_drops_oldest_reads_with_an_outline_and_counts_the_reread(
        tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "tool_result_pressure_chars", 30_000)
    files = {f"src/f{i}.js": _source(f"F{i}") for i in range(6)}
    rounds = [[("read_file", {"path": p})] for p in files]
    rounds.append([("read_file", {"path": "src/f0.js"})])       # comes back for f0
    model = ScriptedModel(rounds)
    events = await run(monkeypatch, model, _dispatch_files(files))
    stub = next(c for _r, c in model.seen[-1] if "src/f0.js" in c and "dropped" in c)
    assert "whole file" in stub and "Outline:" in stub
    assert "L1 F0" in stub and "L2 update" in stub and "buildF0" in stub
    assert "offset and limit" in stub
    st = _stats(events)
    assert st["evictions"] >= 1 and st["rereads"] == 1
    # the newest reads are still whole
    assert any(c == files["src/f5.js"] for _r, c in model.seen[-1])


def test_stale_reads_go_first_and_reads_of_a_file_being_edited_last(monkeypatch):
    monkeypatch.setattr(settings, "tool_result_pressure_chars", 39_000)
    msgs, tool_msgs = [{"role": "system", "content": "s"}], []

    def add(name, rnd, path, chars):
        msgs.append({"role": "tool", "content": "x" * chars})
        entry = {"idx": len(msgs) - 1, "round": rnd, "name": name, "path": path}
        if name == "read_file":
            entry["span"] = (1, float("inf"))
        tool_msgs.append(entry)
    add("read_file", 0, "b.js", 10_000)      # old, never edited
    add("read_file", 1, "a.js", 10_000)      # read BEFORE a.js was edited: stale
    add("edit_file", 2, "a.js", 50)
    add("read_file", 3, "f.js", 10_000)      # f.js was edited in round 3, then re-read
    add("read_file", 3, "c.js", 10_000)
    edited = {"a.js": 2, "f.js": 3}
    dropped = loop_mod._evict_stale_results(msgs, tool_msgs, current_round=9, edited=edited)
    names = [t["path"] for t in dropped]
    assert names[0] == "a.js"                    # stale first, though b.js is older
    assert "f.js" not in names                   # the file under edit keeps its read
    assert msgs[tool_msgs[0]["idx"]]["content"].startswith("[read_file b.js")


async def test_turn_stats_count_recoveries_retries_and_the_cap(tmp_env, monkeypatch):
    class Model:
        def __init__(self):
            self.n = 0

        async def complete(self, messages, tools=None, **kw):
            self.n += 1
            if self.n == 1:      # DSML recovery rebuilt this call
                yield {"type": "message", "content": "", "usage": None, "tool_calls": [
                    {"id": "dsml_0", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}]}
            elif self.n == 2:    # markup the gateway could not parse
                yield {"type": "message", "usage": None, "tool_calls": [],
                       "content": "<｜DSML｜ invoke name=\"read_file\">"}
            else:                # a call on the last round, tools withheld
                yield {"type": "message", "content": "", "usage": None, "tool_calls": [
                    {"id": f"c{self.n}", "type": "function",
                     "function": {"name": "read_file", "arguments": "{}"}}]}

    async def dispatch(name, args):
        return "ok"
    events = await run(monkeypatch, Model(), dispatch, max_iterations=3)
    st = _stats(events)
    assert events[-2]["type"] == "turn_stats" and events[-1]["type"] == "final"
    assert st["dsml_recovered"] == 1 and st["markup_retries"] == 1
    assert st["forced_conclusion"] == 1 and st["cap_hit"] == 1 and st["stop"] == "cap"
    assert st["rounds"] == 3


# --- RUNS-14: an orchestrator waits with plan_status, not sleep in run_code ---------

async def test_orchestrator_sleep_in_run_code_points_to_plan_status(tmp_env, monkeypatch):
    """conv 500: 16 run_code calls began 'sleep 240-285' (one hit the 300 s
    kill), while plan_status(wait_seconds) wakes on any change or message."""
    ran = []

    async def dispatch(name, args):
        ran.append((name, args))
        return "ok"
    model = ScriptedModel([[("run_code", {"command": "sleep 240; ls dist"})],
                           [("run_code", {"command": "sleep 5; ls dist"})],
                           [("run_code", {"code": "import time\ntime.sleep(280)\nprint(1)"})]])
    events = await run(monkeypatch, model, dispatch,
                       tools=("run_code", "plan_status"), read_only=())
    results = [e["result"] for e in events if e["type"] == "tool_result"]
    assert results[0].startswith("error:") and "plan_status" in results[0]
    assert "wait_seconds" in results[0]
    assert results[1] == "ok"                      # a short pause is fine
    assert results[2].startswith("error:")         # python's time.sleep too
    assert [n for n, _a in ran] == ["run_code"]


async def test_sleep_in_run_code_is_fine_without_plan_status(tmp_env, monkeypatch):
    async def dispatch(name, args):
        return "ok"
    model = ScriptedModel([[("run_code", {"command": "sleep 240"})]])
    events = await run(monkeypatch, model, dispatch, tools=("run_code",), read_only=())
    assert [e["result"] for e in events if e["type"] == "tool_result"] == ["ok"]


async def test_a_budget_stop_shows_in_the_stats(tmp_env, monkeypatch):
    class Spent:
        async def complete(self, messages, tools=None, **kw):
            raise BudgetExceeded("token budget spent")
            yield  # pragma: no cover

    async def dispatch(name, args):
        return "ok"
    events = await run(monkeypatch, Spent(), dispatch)
    assert _stats(events)["stop"] == "budget"
