"""Security-event context boards (backend/secctx.py).

The Review Center's alerts used to be a summary plus a JSON dump. These tests
pin the thing that replaced it: for each event kind, the board must carry the
actual evidence (the flagged lines, the diff, the directory, the traffic) and
must never blow up on a malformed or stale event — a board that 500s is worse
than the JSON it replaced.
"""
import json

import pytest

from backend import db as db_mod
from backend import diffgate, gitgate, secctx, security, writes
from backend.config import settings


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    (settings.projects_dir / "proj").mkdir(parents=True)
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


def _sections(board, kind):
    return [s for s in board["sections"] if s["type"] == kind]


def _text(board):
    return json.dumps(board)


# --- diffgate line recording (what makes a code snippet possible) ------------

def test_scan_records_line_numbers():
    new = "x = 1\ny = 2\nimport socket\n"
    flags = diffgate.scan("x = 1\ny = 2\n", new, "a.py")
    imp = next(f for f in flags if f["trigger"] == "new_import")
    assert imp["detail"]["lines"] == [3]


def test_scan_line_numbers_are_positions_in_the_new_file():
    old = "a = 1\n"
    new = "a = 1\n" + "pad\n" * 20 + "import requests\nrequests.get('http://x')\n"
    flags = {f["trigger"]: f["detail"] for f in diffgate.scan(old, new, "a.py")}
    assert flags["new_import"]["lines"] == [22]
    assert flags["network_call"]["lines"] == [23]


def test_locate_refinds_lines_in_a_changed_file():
    text = "# header\nimport socket\n"
    assert diffgate.locate(text, "new_import", {"modules": ["socket"]}) == [2]
    assert diffgate.locate(text, "new_import", {"modules": ["os"]}) == []


# --- write_flag board --------------------------------------------------------

async def test_write_board_shows_the_flagged_code(db):
    body = b"def go():\n    pass\n\n\nimport socket\nsocket.socket()\n"
    await writes.apply_write("proj", "mod.py", body)
    ev = (await security.list_events(db))[0]

    board = await secctx.build_board(db, ev)
    code = _sections(board, "code")
    assert code, "the board must show the code, not just name the file"
    marked = [ln for b in code[0]["blocks"] for ln in b["lines"] if ln["mark"]]
    assert marked and any("socket" in ln["text"] for ln in marked)
    # ...with surrounding context, so the operator sees the code in place
    assert any(not ln["mark"] for b in code[0]["blocks"] for ln in b["lines"])
    assert board["title"] and board["why"] and board["checks"]


async def test_write_board_lists_the_whole_directory(db):
    d = settings.projects_dir / "proj" / "pkg"
    d.mkdir()
    (d / "sibling.py").write_text("x = 1\n")
    (d / "notes.md").write_text("hi\n")
    await writes.apply_write("proj", "pkg/mod.py", b"import socket\n")
    ev = (await security.list_events(db))[0]

    board = await secctx.build_board(db, ev)
    files = _sections(board, "files")[0]
    names = {e["name"] for e in files["entries"]}
    assert {"mod.py", "sibling.py", "notes.md"} <= names
    subject = next(e for e in files["entries"] if e["name"] == "mod.py")
    assert subject["subject"] is True
    # siblings written in the same burst are called out
    assert next(e for e in files["entries"] if e["name"] == "sibling.py")["burst"]


async def test_write_board_classifies_new_modules(db):
    """The question behind a new_import alert is 'has this project ever asked
    for that package' — the board answers it instead of asking the operator."""
    (settings.projects_dir / "proj" / "requirements.txt").write_text("httpx==0.27\n")
    await writes.apply_write("proj", "a.py", b"import socket\nimport httpx\nimport reqeusts\n")
    ev = next(e for e in await security.list_events(db)
              if e["detail"]["trigger"] == "new_import")

    board = await secctx.build_board(db, ev)
    rows = {r[0]: r[1] for r in
            next(t for t in _sections(board, "table") if t["title"] == "The modules")["rows"]}
    assert rows["socket"] == "python stdlib"      # not a dependency at all
    assert rows["httpx"] == "declared"            # the project asked for it
    assert rows["reqeusts"] == "NOT DECLARED"     # the typo-squat shape


@pytest.mark.parametrize("mode,expect", [
    ("allowlist", "allowed host"),          # names what IS reachable
    ("denylist", "ANY host except"),        # names what is NOT — the opposite
    ("denyall", "egress is switched off"),
])
async def test_network_call_board_reads_each_egress_mode_correctly(db, mode, expect):
    """A denylist counted as 'N hosts reachable' told the operator the exact
    opposite of the truth on a security board."""
    from backend import egress
    await egress.set_policy(db, "proj", mode=mode, hosts=["evil.test"])
    await writes.apply_write("proj", "a.py", b"import x\nx = requests.get('http://y')\n")
    ev = next(e for e in await security.list_events(db)
              if e["detail"]["trigger"] == "network_call")

    board = await secctx.build_board(db, ev)
    assert "What this code could reach" in _text(board)
    assert expect in _text(board)


async def test_write_board_names_the_run_that_wrote_it(db):
    from backend import runtime
    cur = await db.execute(
        "INSERT INTO conversations(kind, summary) VALUES ('subagent', 'tidy the parser')")
    await db.commit()
    token = runtime.conversation_id.set(cur.lastrowid)
    try:
        await writes.apply_write("proj", "mod.py", b"import socket\n")
    finally:
        runtime.conversation_id.reset(token)

    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    assert "tidy the parser" in _text(board) and "subagent" in _text(board)


async def test_write_board_includes_the_git_diff(db):
    slug = "proj"
    await gitgate.ensure_repo(slug)
    p = settings.projects_dir / slug / "mod.py"
    p.write_text("def go():\n    pass\n")
    await gitgate.run_git(slug, "add", "-A")
    await gitgate.run_git(slug, "commit", "-qm", "baseline")

    await writes.apply_write(slug, "mod.py", b"def go():\n    import socket\n")
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    diffs = _sections(board, "diff")
    assert diffs, "an uncommitted agent write must show as a diff against HEAD"
    added = [ln["text"] for h in diffs[0]["hunks"] for ln in h["lines"] if ln["k"] == "+"]
    assert any("socket" in a for a in added)
    # and the project's whole uncommitted surface, not just this file
    assert "Uncommitted files in this project" in _text(board)


async def test_secret_leak_board_never_shows_the_value(db):
    settings.secrets_path.write_text(json.dumps({"API_KEY": "supersecretvalue123"}))
    with pytest.raises(writes.SecretLeakError):
        await writes.apply_write("proj", "leak.py", b"KEY = 'supersecretvalue123'\n")

    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    blob = _text(board)
    assert "supersecretvalue123" not in blob, "the board must not leak the value"
    assert "API_KEY" in blob                       # the NAME is what the operator needs
    assert "REFUSED" in blob
    assert not _sections(board, "code"), "a refused write has no code to show"


async def test_write_board_admits_drift(db):
    await writes.apply_write("proj", "mod.py", b"import socket\n")
    ev = (await security.list_events(db))[0]
    # the agent kept working on the file after the flag
    p = settings.projects_dir / "proj" / "mod.py"
    p.write_text("# rewritten, no import at all\n")
    import os
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 600))

    board = await secctx.build_board(db, ev)
    assert "Drift" in _text(board)


async def test_write_board_survives_a_missing_file(db):
    await writes.apply_write("proj", "mod.py", b"import socket\n")
    (settings.projects_dir / "proj" / "mod.py").unlink()
    ev = (await security.list_events(db))[0]

    board = await secctx.build_board(db, ev)
    assert "GONE" in _text(board)


async def test_write_board_refuses_a_path_outside_the_project(db):
    """The path lives in a JSON blob; treat it as data, not as a promise."""
    await security.raise_event(db, kind="write_flag", severity="warn", project="proj",
                               summary="write flag: new_import in ../../etc/passwd",
                               detail={"path": "../../etc/passwd", "trigger": "new_import"})
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    assert "Path refused" in _text(board)
    assert not _sections(board, "code")


# --- egress_anomaly board ----------------------------------------------------

async def test_egress_board_shows_the_actual_requests(db):
    from backend import egress
    for path, out in (("/collect", 40000), ("/collect", 51000)):
        await egress.record_event(db, slug="proj", host="x7f2q9z.example.com",
                                  method="POST", path=path, bytes_out=out,
                                  bytes_in=12, verdict="allow", reason="allowlisted")
    await security.raise_event(
        db, kind="egress_anomaly", severity="critical", project="proj",
        summary="high-entropy host x7f2q9z.example.com (entropy 3.90)",
        detail={"host": "x7f2q9z.example.com", "entropy": 3.9, "threshold": 3.5})

    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    tables = {t["title"]: t for t in _sections(board, "table")}
    reqs = tables["Requests to this host"]
    assert len(reqs["rows"]) == 2 and any("/collect" in r for r in reqs["rows"][0])
    assert "Where this project's bytes go" in tables      # the baseline for a spike
    assert "3.5" in _text(board)                          # the threshold, not just the value


async def test_egress_beacon_board_shows_the_cadence(db):
    from backend import egress
    for _ in range(4):
        await egress.record_event(db, slug="proj", host="c2.example.com",
                                  method="GET", path="/ping", verdict="allow")
    await security.raise_event(
        db, kind="egress_anomaly", severity="critical", project="proj",
        summary="beacon-like cadence to c2.example.com (~60s, cv 0.01)",
        detail={"host": "c2.example.com", "period_seconds": 60.0, "cv": 0.01, "hits": 5})
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    assert "Gaps between the last requests" in _text(board)
    assert "Interval" in _text(board)


# --- login / computer-use / unknown kinds ------------------------------------

async def test_login_board_says_whether_the_account_is_real(db):
    await db.execute("INSERT INTO users(username, password_hash) VALUES ('grant', 'x')")
    await db.commit()
    await security.raise_event(db, kind="login_failed", severity="warn",
                               summary="9 failed logins for 'grant' (from 10.0.0.9)",
                               detail={"username": "grant", "attempts": 9,
                                       "peer": "10.0.0.9"})
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    assert "YES" in _text(board), "a real username is the whole point of the alert"

    await security.raise_event(db, kind="login_failed", severity="warn",
                               summary="4 failed logins for 'admin' (from 10.0.0.9)",
                               detail={"username": "admin", "attempts": 4,
                                       "peer": "10.0.0.9"})
    ev = (await security.list_events(db))[0]
    assert "no such user" in _text(await secctx.build_board(db, ev))


async def test_computeruse_board_lists_connected_clients(db):
    await security.raise_event(db, kind="computeruse_auth", severity="warn",
                               summary="3 rejected computer-use pairing attempts",
                               detail={"peer": "192.168.1.40", "attempts": 3})
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    assert "Computers connected right now" in _text(board)
    assert "your LAN" in _text(board)        # a private peer is framed as probably yours


async def test_unknown_kind_still_gets_a_readable_board(db):
    await security.raise_event(db, kind="image_stale", severity="info",
                               summary="the golden image is 40 days old",
                               detail={"age_days": 40, "note": "rebuild it"})
    ev = (await security.list_events(db))[0]
    board = await secctx.build_board(db, ev)
    facts = _sections(board, "facts")[0]
    labels = {r["label"] for r in facts["rows"]}
    assert "Age days" in labels               # detail keys become labelled facts
    assert board["title"] == "Image stale"
    assert "rebuild it" in _text(board)


async def test_board_never_raises_on_a_junk_detail(db):
    for detail in ('"just a string"', "[1, 2, 3]", "null", "{bad json"):
        await db.execute("INSERT INTO security_events(kind, summary, detail) "
                         "VALUES ('write_flag', 's', ?)", (detail,))
    await db.commit()
    for ev in await secctx_events(db):
        board = await secctx.build_board(db, ev)
        assert board["sections"] is not None


async def secctx_events(db):
    return await security.list_events(db, limit=4)
