"""The Queue as one card per run (SB2, 2026-10-01): group keys, counts per kind,
the worst tier on top, "what the agent was doing", auto-resolve of info-only
groups when their run ends, and the two read endpoints."""
import json

import httpx
import pytest

from backend import db as db_mod
from backend import secruns, security
from backend.auth import hash_password
from backend.main import app


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    security._pings.clear()
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
async def client(db):
    await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                     ("grindlewalt", hash_password("hunter2")))
    await db.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "grindlewalt", "password": "hunter2"})
        yield c


async def _conv(db, cid, parent=None, kind="chat", job=None, summary=None, started=None):
    await db.execute("INSERT INTO conversations(id, parent_conversation_id, kind, job_id, summary,"
                     " started_at) VALUES (?,?,?,?,?, COALESCE(?, datetime('now')))",
                     (cid, parent, kind, job, summary, started))
    await db.commit()


async def _call(db, cid, tool, args, result="ok", call_id=None, at=None):
    cur = await db.execute("INSERT INTO tool_calls(conversation_id, tool, args, result, call_id, "
                           "created_at) VALUES (?,?,?,?,?, COALESCE(?, datetime('now')))",
                           (cid, tool, json.dumps(args), result, call_id, at))
    await db.commit()
    return cur.lastrowid


async def _ev(db, kind="unexpected_process", severity="warn", **kw):
    kw.setdefault("summary", f"{kind} event")
    return await security.raise_event(db, kind=kind, severity=severity, **kw)


def _by_key(out):
    return {c["key"]: c for c in out["runs"]}


# --- the grouping rules ---------------------------------------------------------------

def test_the_key_rules_in_order():
    k = secruns.key_for
    assert k({"kind": "x", "project_slug": "p"}, 500) == ("run:500", "run")
    assert k({"kind": "x", "box_id": "p-p", "boot_id": "abcdef0123456789"}, None) \
        == ("box:p-p:abcdef01", "box")
    assert k({"kind": "x", "detail": {"box_id": "p-p"}}, None) == ("box:p-p", "box")
    assert k({"kind": "login_failed", "project_slug": "p"}, None) == ("proj:p:login_failed", "proj")
    assert k({"kind": "login_failed"}, None) == ("kind:login_failed", "proj")


async def test_events_of_one_run_share_a_card_and_a_subagent_files_under_its_chat(db):
    await _conv(db, 500, summary="Realistic Minecraft in the browser")
    await _conv(db, 501, parent=500, kind="agent", job="j1")
    await _conv(db, 502, parent=501, kind="agent", job="j1")
    await _ev(db, project="bg", conversation_id=500, summary="New program: node",
              detail={"exe": "/usr/bin/node", "box_id": "p-bg"})
    await _ev(db, project="bg", conversation_id=502, summary="New program: crashpad",
              detail={"exe": "/opt/crashpad", "box_id": "p-bg"})
    await _ev(db, kind="write_flag", project="bg", conversation_id=501, summary="wf",
              detail={"path": "tests/world.test.mjs"})
    await _ev(db, kind="write_flag", project="other", conversation_id=None, summary="no run",
              detail={"path": "x.py"})
    out = await secruns.list_runs(db)
    cards = _by_key(out)
    assert set(cards) == {"run:500", "proj:other:write_flag"}
    run = cards["run:500"]
    assert run["counts"]["need"] == 3 and run["project"] == "bg"
    assert {k["kind"]: k["n"] for k in run["kinds"]} == {"unexpected_process": 2, "write_flag": 1}
    assert run["title"].startswith('bg · chat 500 "Realistic Minecraft')
    up = next(k for k in run["kinds"] if k["kind"] == "unexpected_process")
    assert sorted(up["subjects"]) == ["crashpad", "node"]


async def test_old_rows_group_by_the_conversation_in_their_detail_then_the_box(db):
    """Nothing is backfilled: a row from before the columns has detail.conversation_id (a
    write flag) or only a box (a process alert)."""
    await _conv(db, 600)
    await db.execute("INSERT INTO security_events(kind, severity, project_slug, summary, detail) "
                     "VALUES ('write_flag','warn','p','old wf', ?)",
                     (json.dumps({"path": "a.py", "conversation_id": 600}),))
    await db.execute("INSERT INTO security_events(kind, severity, summary, detail) "
                     "VALUES ('unexpected_process','warn','old proc', ?)",
                     (json.dumps({"box_id": "p-p", "exe": "/bin/x"}),))
    await db.commit()
    assert set(_by_key(await secruns.list_runs(db))) == {"run:600", "box:p-p"}


async def test_a_card_is_headed_by_its_worst_tier_and_cards_sort_worst_first(db):
    await _conv(db, 1)
    await _conv(db, 2)
    await _ev(db, project="a", conversation_id=1)                                  # alert
    await _ev(db, kind="write_flag", project="a", conversation_id=1, summary="w")  # alert
    await _ev(db, kind="egress_anomaly", severity="critical", project="b", conversation_id=2,
              summary="cut")                                                      # critical
    out = await secruns.list_runs(db)
    assert [c["key"] for c in out["runs"]] == ["run:2", "run:1"]
    assert (out["runs"][0]["tier"], out["runs"][0]["severity"]) == ("critical", "critical")
    assert out["runs"][1]["tier"] == "alert"
    assert out["totals"] == {"runs": 2, "need": 3, "reports": 0}


async def test_filtered_rows_are_counted_agent_reports_apart_and_record_rows_hidden(db):
    await _conv(db, 10)
    for i in range(4):          # a rule files these quietly: acknowledged, quiet='rule'
        await _ev(db, kind="write_flag", project="p", conversation_id=10, summary=f"scratch {i}",
                  rule="a file this run made", detail={"path": f"s{i}.py"})
    await _ev(db, kind="write_flag", project="p", conversation_id=10, summary="real",
              detail={"path": "real.py"})
    await _ev(db, kind="harness_fault", severity="info", conversation_id=10, summary="tool broke")
    await _ev(db, kind="browser_session", severity="info", conversation_id=10, summary="b",
              detail={"device_id": 1})
    run = _by_key(await secruns.list_runs(db))["run:10"]
    assert run["counts"] == {"need": 1, "reports": 1, "filtered": 4, "record": 1}
    d = await secruns.run_detail(db, "run:10")
    assert len(d["filtered"]) == 4 and d["filtered"][0]["rule"] == "a file this run made"
    assert {e["kind"] for e in d["events"]} == {"write_flag", "harness_fault"}


async def test_a_group_with_only_filtered_rows_is_not_in_the_queue(db):
    await _conv(db, 11)
    await _ev(db, kind="write_flag", project="p", conversation_id=11, summary="s", rule="r",
              detail={"path": "s.py"})
    assert (await secruns.list_runs(db))["runs"] == []
    assert "run:11" in _by_key(await secruns.list_runs(db, queue=False))


async def test_a_kind_the_operator_set_to_record_leaves_the_card(db):
    await _conv(db, 12)
    await _ev(db, project="p", conversation_id=12)
    await security.set_prefs(db, kinds={"unexpected_process": "record"})
    assert (await secruns.list_runs(db))["runs"] == []


# --- what the agent was doing ---------------------------------------------------------

async def test_the_step_is_found_by_the_models_call_id(db):
    await _conv(db, 20, summary="Probe the screenshot tool")
    await _call(db, 20, "run_code", {"command": "ls"}, call_id="call_1")
    step = await _call(db, 20, "run_code", {"command": "pkill -f 'serve.mjs'; nohup node scripts/serve.mjs &"},
                       call_id="call_2")
    await _call(db, 20, "read_file", {"path": "a"}, call_id="call_3")
    await _ev(db, project="p", conversation_id=20, call_id="call_2", detail={"cmd": "node x"})
    d = (await secruns.run_detail(db, "run:20"))["events"][0]["doing"]
    assert d["step"]["id"] == step and d["step"]["match"] == "call" and d["step"]["tool"] == "run_code"
    assert d["step"]["command"].startswith("pkill -f 'serve.mjs'")
    assert d["untrusted"] is True


async def test_a_process_is_matched_to_the_call_that_named_its_script(db):
    await _conv(db, 21)
    started = await _call(db, 21, "run_code",
                          {"command": "nohup node scripts/serve.mjs > /tmp/x.log 2>&1 &"})
    await _call(db, 21, "read_file", {"path": "notes.md"})
    await _call(db, 21, "list_files", {})
    await _ev(db, project="p", conversation_id=21,
              detail={"cmd": "node scripts/serve.mjs", "exe": "/usr/bin/node", "box_id": "p"})
    step = (await secruns.run_detail(db, "run:21"))["events"][0]["doing"]["step"]
    assert (step["id"], step["match"]) == (started, "command")


async def test_without_a_match_the_nearest_earlier_step_is_named_as_nearest(db):
    await _conv(db, 22)
    await _call(db, 22, "read_file", {"path": "a"}, at="2020-01-01 00:00:00")
    last = await _call(db, 22, "edit_file", {"path": "b"}, at="2020-01-01 00:00:05")
    await _call(db, 22, "run_code", {"command": "later"}, at="2099-01-01 00:00:00")
    await _ev(db, kind="write_flag", project="p", conversation_id=22, summary="w")
    # the event is raised now: the 2099 call is in the future
    await db.execute("UPDATE security_events SET created_at = '2020-01-01 00:00:06'")
    await db.commit()
    step = (await secruns.run_detail(db, "run:22"))["events"][0]["doing"]["step"]
    assert (step["id"], step["match"]) == (last, "nearest")


async def test_says_is_the_narration_before_the_step_then_a_todo_then_the_title(db):
    await _conv(db, 23, summary="Build the world")
    await _call(db, 23, "todo_update", {"action": "add", "items": ["map the chunks", "mesh them"]})
    prev = await _call(db, 23, "read_file", {"path": "a"})
    step = await _call(db, 23, "run_code", {"command": "node serve.mjs"}, call_id="c9")
    await db.execute("INSERT INTO turn_narration(conversation_id, after_call_id, text) "
                     "VALUES (23, ?, 'Probe the screenshot tool: start serve.mjs in the box')",
                     (prev,))
    await db.commit()
    await _ev(db, project="p", conversation_id=23, call_id="c9")
    d = (await secruns.run_detail(db, "run:23"))["events"][0]["doing"]
    assert d["step"]["id"] == step
    assert d["says"] == {"text": "Probe the screenshot tool: start serve.mjs in the box",
                         "source": "narration"}
    await db.execute("DELETE FROM turn_narration")
    await db.commit()
    assert (await secruns.run_detail(db, "run:23"))["events"][0]["doing"]["says"] == {
        "text": "map the chunks / mesh them", "source": "todo"}
    await db.execute("DELETE FROM tool_calls WHERE tool = 'todo_update'")
    await db.commit()
    assert (await secruns.run_detail(db, "run:23"))["events"][0]["doing"]["says"] == {
        "text": "Build the world", "source": "chat title"}


async def test_agent_text_is_clipped_and_stripped_of_control_characters(db):
    await _conv(db, 24)
    await _call(db, 24, "run_code", {"command": "echo \x1b[31mred\x1b[0m " + "x" * 2000}, call_id="c")
    await _ev(db, project="p", conversation_id=24, call_id="c")
    step = (await secruns.run_detail(db, "run:24"))["events"][0]["doing"]["step"]
    assert len(step["command"]) <= secruns.CLIP_COMMAND and "\x1b" not in step["command"]


async def test_an_event_with_no_run_has_no_doing(db):
    await _ev(db, kind="login_failed", project="p", summary="burst")
    ev = (await secruns.run_detail(db, "proj:p:login_failed"))["events"][0]
    assert ev["doing"] is None


# --- auto-resolve of info-only groups when the run ends -------------------------------

async def test_an_info_only_group_resolves_when_its_run_has_ended(db, monkeypatch):
    from backend import chat
    await _conv(db, 30, started="2020-01-01 00:00:00")
    await _ev(db, kind="harness_fault", severity="info", conversation_id=30, summary="report")
    monkeypatch.setattr(chat, "_running_loops", lambda: {30})
    assert len((await secruns.list_runs(db))["runs"]) == 1          # still running: it stays
    monkeypatch.setattr(chat, "_running_loops", lambda: set())
    assert (await secruns.list_runs(db))["runs"] == []              # ended and settled
    async with db.execute("SELECT acknowledged FROM security_events") as cur:
        assert [r["acknowledged"] for r in await cur.fetchall()] == [1]


async def test_a_run_that_just_stopped_has_not_ended_yet(db, monkeypatch):
    from backend import chat
    await _conv(db, 31)
    await _call(db, 31, "run_code", {"command": "x"})              # activity just now
    await _ev(db, kind="harness_fault", severity="info", conversation_id=31, summary="report")
    monkeypatch.setattr(chat, "_running_loops", lambda: set())
    assert len((await secruns.list_runs(db))["runs"]) == 1


async def test_a_group_holding_a_real_alert_never_auto_resolves(db, monkeypatch):
    from backend import chat
    await _conv(db, 32, started="2020-01-01 00:00:00")
    await _ev(db, kind="harness_fault", severity="info", conversation_id=32, summary="report")
    await _ev(db, project="p", conversation_id=32)
    monkeypatch.setattr(chat, "_running_loops", lambda: set())
    run = _by_key(await secruns.list_runs(db))["run:32"]
    assert run["counts"]["need"] == 1 and run["counts"]["reports"] == 1


async def test_when_liveness_cannot_be_known_nothing_resolves(db, monkeypatch):
    from backend import chat
    await _conv(db, 33, started="2020-01-01 00:00:00")
    await _ev(db, kind="harness_fault", severity="info", conversation_id=33, summary="report")

    def boom():
        raise RuntimeError("no registry")
    monkeypatch.setattr(chat, "_running_loops", boom)
    assert len((await secruns.list_runs(db))["runs"]) == 1


# --- endpoints ------------------------------------------------------------------------

async def test_runs_and_a_run_over_http_and_the_group_ack(db, client):
    await _conv(db, 40, summary="a chat")
    await _ev(db, project="p", conversation_id=40, detail={"exe": "/usr/bin/node"})
    await _ev(db, kind="harness_fault", severity="info", conversation_id=40, summary="rep")
    r = await client.get("/api/security/runs")
    assert r.status_code == 200 and r.json()["runs"][0]["key"] == "run:40"
    d = (await client.get("/api/security/runs/run:40")).json()
    assert len(d["events"]) == 2 and d["title"].startswith('p · chat 40 "a chat"')
    assert (await client.get("/api/security/runs/run:999")).status_code == 404
    a = await client.post("/api/security/runs/run:40/ack", json={"only": "reports"})
    assert a.json() == {"ok": True, "done": 1}
    assert (await client.get("/api/security/runs/run:40")).json()["counts"]["reports"] == 0
    assert (await client.post("/api/security/runs/run:40/ack", json={"only": "x"})).status_code == 400
    assert (await client.post("/api/security/runs/run:40/ack")).json()["done"] == 1
    assert (await client.get("/api/security/runs")).json()["runs"] == []


async def test_the_existing_endpoints_still_work(db, client):
    await _ev(db, kind="write_flag", project="p", summary="w")
    r = await client.get("/api/security/events?unacknowledged=true&queue=true")
    assert r.status_code == 200 and len(r.json()["events"]) == 1
