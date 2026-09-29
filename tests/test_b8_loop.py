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
