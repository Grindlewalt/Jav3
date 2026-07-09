"""Token/latency optimizations: tool-result cap + eviction, rules pre-filter,
model retry, project-context budget, headless-agent discipline, report
compaction, registry staleness, chat tool subsetting, research parallel reads,
and the funnel endpoint wiring."""
import httpx
import pytest

from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


# --- loop: tool-result cap + eviction ----------------------------------------

class _FakeModel:
    """Scripted model: emits tool calls for `rounds` rounds, then a final
    answer. Snapshots the message list it was handed on every call."""
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0
        self.seen: list[list[str]] = []

    async def complete(self, messages, tools=None, **kw):
        self.seen.append([str(m.get("content")) for m in messages])
        self.calls += 1
        if self.calls <= self.rounds:
            yield {"type": "message", "content": "",
                   "tool_calls": [{"id": f"c{self.calls}", "type": "function",
                                   "function": {"name": "fake_tool",
                                                "arguments": "{}"}}],
                   "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [],
                   "usage": None}


async def _run_loop(monkeypatch, fake_model, tool_result):
    from backend.agent import loop as loop_mod
    from backend.agent.tools import registry

    async def fake_dispatch(name, args):
        return tool_result

    monkeypatch.setattr(loop_mod, "model", fake_model)
    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        events = []
        async for ev in loop_mod.run_turn(
                db, cid, "system", [{"role": "user", "content": "go"}],
                tools=[{"type": "function",
                        "function": {"name": "fake_tool", "parameters": {}}}]):
            events.append(ev)
        return events
    finally:
        await db.close()


async def test_tool_result_capped_in_context(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "tool_result_max_chars", 1000)
    fake = _FakeModel(rounds=1)
    await _run_loop(monkeypatch, fake, "x" * 20_000)
    # the model's second call sees the tool result already truncated
    tool_msg = next(m for m in fake.seen[1] if m.startswith("x"))
    assert len(tool_msg) < 1200
    assert "truncated: 20,000 chars total" in tool_msg


async def test_stale_big_tool_results_evicted(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "tool_result_keep_recent", 1)
    monkeypatch.setattr(settings, "tool_result_evict_chars", 100)
    fake = _FakeModel(rounds=3)
    events = await _run_loop(monkeypatch, fake, "y" * 500)
    assert events[-1] == {"type": "final", "content": "done"}
    # by the 3rd model call (round index 2), round-0's big result is a stub
    third = fake.seen[2]
    assert any("was dropped to keep context small" in m for m in third)
    # the most recent round's result is still intact
    assert any(m == "y" * 500 for m in third)


async def test_small_tool_results_not_evicted(tmp_env, monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "tool_result_keep_recent", 1)
    monkeypatch.setattr(settings, "tool_result_evict_chars", 100)
    fake = _FakeModel(rounds=3)
    await _run_loop(monkeypatch, fake, "small")
    assert not any("dropped to keep context" in m for m in fake.seen[-1])


# --- loop: rules pre-filter ----------------------------------------------------

def test_quick_rules_verdict():
    from backend.agent.loop import _quick_rules_verdict
    rules = ("# Operator rules (non-negotiable): apply to THIS reply\n"
             "Follow every rule below exactly.\n"
             '- Never use em dashes. Wrong: "a — b". Right: "a, b".')
    assert _quick_rules_verdict("clean text, no dash", rules) is False
    assert _quick_rules_verdict("bad — text", rules) is True
    # an uncheckable rule forces the model pass
    rules2 = rules + "\n- Always answer in pirate speak"
    assert _quick_rules_verdict("clean text", rules2) is None


# --- model: retry with backoff -------------------------------------------------

async def test_model_retries_transient_then_succeeds(monkeypatch):
    from backend.agent.model import Model
    monkeypatch.setattr(settings, "model_retries", 2)
    monkeypatch.setattr(settings, "model_retry_backoff_seconds", 0)
    calls = {"n": 0}

    async def fake_stream(self, base, key, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        yield {"type": "raw", "content": "hi", "tool_calls": [], "usage": None}

    monkeypatch.setattr(Model, "_stream_once", fake_stream)
    m = Model(api_key="test")
    out = [ev async for ev in m.complete([{"role": "user", "content": "x"}])]
    assert calls["n"] == 2
    assert out[-1]["content"] == "hi"


async def test_model_does_not_retry_4xx(monkeypatch):
    from backend.agent.model import Model, ModelError
    monkeypatch.setattr(settings, "model_retries", 2)
    monkeypatch.setattr(settings, "model_retry_backoff_seconds", 0)
    calls = {"n": 0}

    async def fake_stream(self, base, key, payload):
        calls["n"] += 1
        raise ModelError("model API 400: bad request", status=400)
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(Model, "_stream_once", fake_stream)
    m = Model(api_key="test")
    with pytest.raises(ModelError):
        async for _ in m.complete([{"role": "user", "content": "x"}]):
            pass
    assert calls["n"] == 1


async def test_model_does_not_retry_after_tokens_streamed(monkeypatch):
    from backend.agent.model import Model
    monkeypatch.setattr(settings, "model_retries", 2)
    monkeypatch.setattr(settings, "model_retry_backoff_seconds", 0)
    calls = {"n": 0}

    async def fake_stream(self, base, key, payload):
        calls["n"] += 1
        yield {"type": "token", "text": "partial"}
        raise httpx.ReadError("dropped mid-stream")

    monkeypatch.setattr(Model, "_stream_once", fake_stream)
    m = Model(api_key="test")
    with pytest.raises(httpx.ReadError):
        async for _ in m.complete([{"role": "user", "content": "x"}]):
            pass
    assert calls["n"] == 1   # a retry would duplicate the streamed token


# --- memory: active-project context budget --------------------------------------

async def test_project_context_budget(tmp_env, monkeypatch):
    from backend.memory import assemble_system_prompt, set_context_selection
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# Demo\n\n## Summary\nsmall journal\n")
    (proj / "big.txt").write_text("B" * 40_000)
    (proj / "small.txt").write_text("tiny contents here")
    set_context_selection("demo", ["big.txt", "small.txt"])
    monkeypatch.setattr(settings, "project_context_budget_tokens", 500)

    prompt = await assemble_system_prompt(None, active="demo")
    assert "small journal" in prompt                       # project.md always in
    assert "tiny contents here" in prompt                  # fits the budget
    assert "B" * 1000 not in prompt                        # big file not inlined
    assert "NOT inlined" in prompt and "big.txt" in prompt  # ...but indexed


async def test_project_context_uncapped_when_it_fits(tmp_env):
    from backend.memory import assemble_system_prompt, set_context_selection
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# Demo\n\n## Summary\nhi\n")
    (proj / "a.txt").write_text("alpha contents")
    set_context_selection("demo", ["a.txt"])
    prompt = await assemble_system_prompt(None, active="demo")
    assert "alpha contents" in prompt and "NOT inlined" not in prompt


# --- agents: headless iteration cap + report compaction -------------------------

async def test_headless_agent_gets_subagent_cap(client, monkeypatch):
    from backend import agents_run

    seen = {}

    async def fake_run_turn(db, cid, system_prompt, history, **kw):
        seen.update(kw)
        yield {"type": "final", "content": "ok"}

    monkeypatch.setattr(agents_run, "run_turn", fake_run_turn)
    await client.post("/api/agents", json={"name": "Scout"})
    r = await agents_run.run_agent_headless("scout", "look around")
    assert r["final"] == "ok"
    assert seen["max_iterations"] == settings.subagent_max_iterations

    # a definition can grant more rounds
    a = (await client.get("/api/agents/scout")).json()
    a["max_iterations"] = 20
    await client.put("/api/agents/scout", json=a)
    await agents_run.run_agent_headless("scout", "look again")
    assert seen["max_iterations"] == 20


async def test_compact_report_passthrough_and_fallback(tmp_env, monkeypatch):
    from backend import agents_run
    short = "all done"
    assert await agents_run.compact_report("A", "t", short, 1) == short

    async def failing_complete(*a, **k):
        raise RuntimeError("no api")
        yield  # pragma: no cover

    monkeypatch.setattr(agents_run.model, "complete", failing_complete)
    long = "z" * 10_000
    out = await agents_run.compact_report("A", "t", long, 7)
    assert out.startswith("z" * 100)
    assert "truncated: 10,000 chars" in out and "conversation 7" in out


async def test_compact_report_summarizes(tmp_env, monkeypatch):
    from backend import agents_run

    async def fake_complete(messages, **kw):
        yield {"type": "message", "content": "the tight summary", "tool_calls": []}

    monkeypatch.setattr(agents_run.model, "complete", fake_complete)
    out = await agents_run.compact_report("A", "t", "z" * 10_000, 7)
    assert "the tight summary" in out and "compacted from 10,000 chars" in out


# --- registry: staleness + requires_project flag ---------------------------------

def test_registry_recompiles_when_toolmd_newer(tmp_env, monkeypatch):
    import os
    from backend.agent.tools import registry
    tools = tmp_env / "tools"
    (tools / "demo_tool").mkdir(parents=True)
    md = tools / "demo_tool" / "TOOL.md"
    md.write_text("---\nname: demo_tool\ndescription: v1\nenabled: true\n---\n")
    monkeypatch.setattr(settings, "tools_dir", tools)
    entries = registry.load_registry()
    assert entries[0]["description"] == "v1"

    md.write_text("---\nname: demo_tool\ndescription: v2\nenabled: true\n---\n")
    reg = settings.data_dir / "registry.json"
    os.utime(md, (reg.stat().st_mtime + 5, reg.stat().st_mtime + 5))
    entries = registry.load_registry()
    assert entries[0]["description"] == "v2"


def test_project_tools_flagged(tmp_env):
    from backend.agent.tools import registry
    by_name = {e["name"]: e for e in registry.compile_registry()}
    for name in ("read_file", "write_file", "run_gated", "search_codebase"):
        assert by_name[name].get("requires_project") is True, name
    for name in ("load_project", "memory_read", "web_search"):
        assert not by_name[name].get("requires_project"), name


# --- research: pages within a reader fetched/summarized concurrently -------------

async def test_reader_parallel_pages(client, monkeypatch):
    import asyncio
    from backend import research, webtools

    await client.post("/api/projects", json={"name": "Demo"})
    in_flight = {"now": 0, "max": 0}

    async def fake_read(url, session=None):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        if "bad" in url:
            return "error: nope"
        return f"text of {url}"

    async def fake_summarize(topic, theme, url, text):
        return f"- bullet for {url}"

    monkeypatch.setattr(webtools, "read", fake_read)
    monkeypatch.setattr(research, "_summarize_page", fake_summarize)
    group = {"theme": "t", "urls": ["https://a", "https://b", "https://bad"]}
    findings = await research._reader("demo", None, "job1", group, "topic", "job1")
    assert "bullet for https://a" in findings and "bullet for https://b" in findings
    assert "https://bad" not in findings.replace("read https://bad", "")
    assert in_flight["max"] >= 2   # pages actually overlapped


# --- funnel endpoint wired --------------------------------------------------------

async def test_funnel_requires_project(client):
    r = await client.post("/api/runs/funnel", json={"brief": "do things"})
    assert r.status_code == 400


async def test_funnel_streams_job(client, monkeypatch):
    from backend import bus, runs_api

    await client.post("/api/projects", json={"name": "Demo"})
    await client.post("/api/projects/demo/load")

    async def fake_run_job(job_id, brief, project, **kw):
        bus.publish(job_id, {"type": "job_final", "job_id": job_id})
        bus.close_job(job_id)
        return {"root_id": 1, "rollup": "done", "doc_path": None}

    monkeypatch.setattr(runs_api.orchestrator, "run_job", fake_run_job)
    # confirm_peak so the test doesn't 409 when run during a peak-pricing window
    r = await client.post("/api/runs/funnel",
                          json={"brief": "map the repo", "confirm_peak": True})
    assert r.status_code == 200
    assert "job_opened" in r.text and "job_final" in r.text
