"""Fix-shaped tool errors: every failure string names the next step."""

import httpx
import pytest

from backend.agent.tools import registry
from backend.agent.tools.toolctx import require_project
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
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def _project():
    return settings.projects_dir / "demo"


def _write_ten_lines() -> None:
    (_project() / "notes.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 11)) + "\n")


# --- read_file ---------------------------------------------------------------

async def test_read_file_slice_happy_path(client):
    _write_ten_lines()
    out = await registry.dispatch("read_file",
                                  {"path": "notes.txt", "offset": 3, "limit": 4})
    rows = out.splitlines()
    assert rows[0] == "(lines 3-6 of 10 — notes.txt)"
    # no per-line number prefixes: edit_file needs exact text
    assert rows[1:] == ["line3", "line4", "line5", "line6"]


async def test_read_file_slice_coerces_digit_strings(client):
    _write_ten_lines()
    out = await registry.dispatch("read_file",
                                  {"path": "notes.txt", "offset": "2", "limit": "2"})
    assert out.splitlines()[0] == "(lines 2-3 of 10 — notes.txt)"
    out = await registry.dispatch("read_file",
                                  {"path": "notes.txt", "offset": "abc"})
    assert out.startswith("error: offset must be an integer")


async def test_read_file_whole_overflow_throws_not_truncates(client, monkeypatch):
    monkeypatch.setattr(settings, "tool_result_max_chars", 120)
    (_project() / "big.txt").write_text("\n".join("x" * 10 for _ in range(30)) + "\n")
    out = await registry.dispatch("read_file", {"path": "big.txt"})
    assert out.startswith("error: big.txt is 30 lines / 330 chars — too big to return whole")
    assert "offset (1-based start line)" in out and "search_codebase" in out
    assert "xxxxxxxxxx" not in out  # no truncated content rides along
    # the escape hatch it names actually works
    out = await registry.dispatch("read_file", {"path": "big.txt", "offset": 1, "limit": 3})
    assert out.splitlines()[0] == "(lines 1-3 of 30 — big.txt)"


async def test_read_file_offset_past_eof(client):
    _write_ten_lines()
    out = await registry.dispatch("read_file", {"path": "notes.txt", "offset": 99})
    assert out == "error: notes.txt has only 10 lines (you asked for offset 99)."


async def test_read_file_missing_with_did_you_mean(client):
    code = _project() / "code"
    code.mkdir(exist_ok=True)
    (code / "utils.py").write_text("x = 1\n")
    out = await registry.dispatch("read_file", {"path": "utils.py"})
    assert out.startswith("error: no such file 'utils.py' in project 'demo'.")
    assert "Did you mean 'code/utils.py'?" in out


async def test_read_file_missing_without_match(client):
    out = await registry.dispatch("read_file", {"path": "nope.txt"})
    assert out.startswith("error: no such file 'nope.txt' in project 'demo'.")
    assert "Use list_files to see what exists." in out


# --- edit_file ----------------------------------------------------------------

async def test_edit_file_find_not_found(client):
    (_project() / "a.txt").write_text("hello world")
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "xyz", "replace": "q"})
    assert out == ("error: 'find' text not found in a.txt. Read the file with "
                   "read_file and copy the exact text, including whitespace "
                   "and indentation.")


async def test_edit_file_ambiguous_match(client):
    (_project() / "a.txt").write_text("x y x")
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "x", "replace": "z"})
    assert out == ("error: 'find' matches 2 places in a.txt. Set all=true to "
                   "replace every occurrence, or extend 'find' with surrounding "
                   "lines to make it unique.")


async def test_edit_file_noop_guard(client):
    (_project() / "a.txt").write_text("hello world")
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "hello", "replace": "hello"})
    assert out == "error: find and replace are identical — no change to make."


async def test_edit_file_success_applies(client):
    (_project() / "a.txt").write_text("hello world")
    out = await registry.dispatch("edit_file",
                                  {"path": "a.txt", "find": "hello", "replace": "goodbye"})
    assert "edited" in out
    assert "goodbye world" in await registry.dispatch("read_file", {"path": "a.txt"})


# --- search_codebase ----------------------------------------------------------

async def test_search_codebase_no_match_is_instructive(client):
    out = await registry.dispatch("search_codebase", {"query": "zzz_not_here"})
    assert out == ("no matches for 'zzz_not_here'. Try a shorter or broader term, "
                   "drop subdir to search the whole project, or set regex=true "
                   "for patterns.")


# --- require_project ------------------------------------------------------------

async def test_require_project_message_names_the_fix(client):
    await client.post("/api/projects/unload")
    with pytest.raises(LookupError) as ei:
        await require_project()
    msg = str(ei.value)
    assert "no project is loaded — call load_project first" in msg
    assert "All projects" in msg
