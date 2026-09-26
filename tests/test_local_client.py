"""/local, client side: the jav3 client's LocalExecutor on a temp directory,
and the TUI answering a `local_tool` event with POST .../local_result."""
import importlib.machinery
import importlib.util
import json
import socket
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parents[1] / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_local", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_local", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()


def _ex(tmp_path, answers=None):
    asked = []

    def approve(kind, summary):
        asked.append((kind, summary))
        return (answers or {}).get(kind, "yes")
    return jav3.LocalExecutor(tmp_path, approve), asked


async def test_read_list_search_never_ask(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("one\ntwo = 2\nthree\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("two = 2")
    ex, asked = _ex(tmp_path)
    ok, out = await ex.run("local_read_file", {"path": "src/a.py", "offset": 2, "limit": 1})
    assert ok and out.startswith("     2\ttwo = 2") and "offset=3" in out
    ok, out = await ex.run("local_list_files", {"path": ".", "depth": 3})
    assert ok and "src/" in out and "a.py" in out and "node_modules" not in out
    ok, out = await ex.run("local_search", {"query": "TWO"})
    assert ok and out == "src/a.py:2: two = 2"
    ok, out = await ex.run("local_search", {"query": r"^th", "regex": True})
    assert ok and "src/a.py:3" in out
    assert asked == []


async def test_read_errors_are_text(tmp_path):
    ex, _ = _ex(tmp_path)
    ok, out = await ex.run("local_read_file", {"path": "missing"})
    assert not ok and "no such file" in out
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    ok, out = await ex.run("local_read_file", {"path": "b.bin"})
    assert not ok and "binary" in out
    ok, out = await ex.run("local_read_file", {"nope": 1})
    assert not ok and "bad arguments" in out
    ok, out = await ex.run("rm_rf", {})
    assert not ok


async def test_write_and_edit_ask_with_a_diff(tmp_path):
    ex, asked = _ex(tmp_path)
    ok, out = await ex.run("local_write_file", {"path": "d/new.txt", "content": "a\nb\n"})
    assert ok and (tmp_path / "d" / "new.txt").read_text() == "a\nb\n"
    assert asked[-1][0] == "write" and "+a" in asked[-1][1]
    ok, out = await ex.run("local_edit_file", {"path": "d/new.txt", "find": "b",
                                               "replace": "c"})
    assert ok and (tmp_path / "d" / "new.txt").read_text() == "a\nc\n"
    assert asked[-1][0] == "edit" and "-b" in asked[-1][1] and "+c" in asked[-1][1]
    (tmp_path / "r.txt").write_text("x x x")
    ok, out = await ex.run("local_edit_file", {"path": "r.txt", "find": "x", "replace": "y"})
    assert not ok and "3 times" in out
    ok, out = await ex.run("local_edit_file", {"path": "r.txt", "find": "x",
                                               "replace": "y", "all": True})
    assert ok and (tmp_path / "r.txt").read_text() == "y y y"


async def test_denied_changes_nothing(tmp_path):
    ex, asked = _ex(tmp_path, {"write": "no", "edit": "no", "shell": "no"})
    ok, out = await ex.run("local_write_file", {"path": "f.txt", "content": "x"})
    assert not ok and "did not approve" in out and not (tmp_path / "f.txt").exists()
    (tmp_path / "g.txt").write_text("keep")
    ok, out = await ex.run("local_edit_file", {"path": "g.txt", "find": "keep",
                                               "replace": "gone"})
    assert not ok and (tmp_path / "g.txt").read_text() == "keep"
    ok, out = await ex.run("local_shell", {"command": "touch ran"})
    assert not ok and "did not approve" in out and not (tmp_path / "ran").exists()
    assert [k for k, _ in asked] == ["write", "edit", "shell"]


async def test_no_approver_fails_closed(tmp_path):
    ex = jav3.LocalExecutor(tmp_path)
    ok, out = await ex.run("local_shell", {"command": "touch ran"})
    assert not ok and not (tmp_path / "ran").exists()


async def test_shell_runs_in_cwd_and_always_sticks_per_kind(tmp_path):
    ex, asked = _ex(tmp_path, {"shell": "always"})
    ok, out = await ex.run("local_shell", {"command": "pwd; echo hi; exit 3"})
    assert ok and out.startswith("exit 3") and "hi" in out
    assert str(tmp_path.resolve()) in out
    ok, out = await ex.run("local_shell", {"command": "echo again"})
    assert ok and "again" in out
    assert [k for k, _ in asked] == ["shell"]          # asked once, then always
    ok, _ = await ex.run("local_write_file", {"path": "w", "content": "1"})
    assert [k for k, _ in asked] == ["shell", "write"]  # a different kind still asks


async def test_shell_timeout_kills(tmp_path):
    ex, _ = _ex(tmp_path)
    ok, out = await ex.run("local_shell", {"command": "sleep 5", "timeout_seconds": 1})
    assert not ok and "timed out" in out


async def test_async_approver(tmp_path):
    async def approve(kind, summary):
        return "yes"
    ex = jav3.LocalExecutor(tmp_path, approve)
    ok, _ = await ex.run("local_write_file", {"path": "a", "content": "1"})
    assert ok and (tmp_path / "a").read_text() == "1"


def test_local_spec(tmp_path):
    s = jav3.local_spec(tmp_path)
    assert s["cwd"] == str(tmp_path.resolve())
    assert s["hostname"] and s["os"] and s["shell"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


async def test_tui_answers_a_local_tool_event(cfg, tmp_path, monkeypatch):
    """/local, send, and the read the server asks for runs here and comes back
    on local_result — keyed by the call id, from this conversation."""
    pytest.importorskip("textual")
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("x = 1\n")
    monkeypatch.chdir(work)
    events = [
        {"type": "start", "conversation_id": 4, "model": "deepseek/deepseek-flash"},
        {"type": "tool", "id": "call_1", "name": "local_read_file", "args": {"path": "a.py"}},
        {"type": "local_tool", "id": "call_1", "name": "local_read_file",
         "args": {"path": "a.py", "offset": 1, "limit": 2000}},
    ]
    posted, results = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path.endswith("/info"):
            return httpx.Response(200, json={
                "title": "t", "files": [], "local": {
                    "cwd": str(work.resolve()), "hostname": socket.gethostname(),
                    "os": "x", "shell": "/bin/sh"}})
        if path == "/api/chat":
            posted.append(json.loads(request.content))
            body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in events)
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/chat/4/local_result":
            results.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "nope"})

    app = jav3.build_tui("http://h:1", "jvd_x", transport=httpx.MockTransport(handler))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.editor.text = "/local"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.local is True
        app.editor.text = "read a.py"
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.05)
            if results:
                break
    assert posted[0]["local"]["cwd"] == str(work.resolve())
    assert results == [{"id": "call_1", "ok": True, "result": "     1\tx = 1"}]
