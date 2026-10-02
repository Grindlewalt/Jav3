"""The read-before-edit guard counts a file the model read through run_code.

Benchmark game 2026-10-01: three edit_file calls were refused with "you haven't
read X" although the agent had cat/sed'd the file in run_code; the guard only
counted read_file."""
import pytest

from backend.agent import loop as loop_mod
from backend.agent.loop import _guard_blind_edit, _paths_read_by
from backend.agent.tools import registry
from backend.db import get_db, init_db

EDIT = '{"path": "src/a.py", "find": "x", "replace": "y"}'


class _Scripted:
    """Emits the given tool-call rounds, then a final answer."""
    def __init__(self, rounds):
        self.rounds, self.call = rounds, 0

    async def complete(self, messages, tools=None, **kw):
        if self.call < len(self.rounds):
            calls = [{"id": f"c{self.call}_{j}", "type": "function",
                      "function": {"name": name, "arguments": args}}
                     for j, (name, args) in enumerate(self.rounds[self.call])]
            self.call += 1
            yield {"type": "message", "content": "", "tool_calls": calls, "usage": None}
        else:
            yield {"type": "message", "content": "done", "tool_calls": [], "usage": None}


async def _turn(monkeypatch, rounds, results=None):
    """Run the scripted rounds; `results` maps a tool name to its fake result.
    Returns the names that reached the registry (a guard refusal never does)."""
    dispatched = []

    async def dispatch(name, args):
        dispatched.append(name)
        r = (results or {}).get(name, "ok")
        return r(args) if callable(r) else r
    monkeypatch.setattr(loop_mod, "model", _Scripted(rounds))
    monkeypatch.setattr(registry, "dispatch", dispatch)
    monkeypatch.setattr(registry, "read_only_names", lambda: frozenset())
    loop_mod._files_seen.clear()
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO conversations (summary) VALUES ('t')")
        cid = cur.lastrowid
        await db.commit()
        async for _ in loop_mod.run_turn(
                cid, "system", [{"role": "user", "content": "go"}],
                tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
                on_tool_call=loop_mod.db_tool_sink(db, cid)):
            pass
    finally:
        await db.close()
    return dispatched


def _rc(cmd: str) -> tuple[str, str]:
    import json
    return ("run_code", json.dumps({"command": cmd}))


OK = "exit 0 · 0.02s\n--- stdout ---\nthe file text"


@pytest.mark.parametrize("cmd", [
    "cat src/a.py",
    "sed -n '1,40p' src/a.py",
    "head -n 30 ./src/a.py",
    "tail -25 src/a.py",
    "nl src/a.py | sed -n 5,9p",
    "cat src/a.py 2>&1 | tail -20",
    "echo ---; cat src/a.py",
    "cat < src/a.py",
])
async def test_a_visible_read_in_run_code_lets_the_edit_through(tmp_env, monkeypatch, cmd):
    await init_db()
    ran = await _turn(monkeypatch, [[_rc(cmd)], [("edit_file", EDIT)]],
                      {"run_code": OK})
    assert ran == ["run_code", "edit_file"]


@pytest.mark.parametrize("cmd,result", [
    ("cat src/a.py | wc -l", OK),                  # the text is not shown
    ("cat src/a.py > /tmp/copy", OK),              # it went into a file
    ("cat src/b.py", OK),                          # another file
    ("sed -i 's/x/y/' src/a.py", OK),              # an edit, not a read
    ("sed 's/x/y/' src/a.py", OK),                 # not a plain print
    ("grep -n x src/a.py", OK),                    # a few lines, not the file
    ("cd lib && cat src/a.py", OK),                # a relative path after a cd
    ("cat src/*.py", OK),                          # a glob is not that exact path
    ("cat src/a.py", "exit 1 · 0.01s\n--- stderr ---\ncat: src/a.py: No such file"),
])
async def test_what_does_not_visibly_read_the_file_still_blocks(
        tmp_env, monkeypatch, cmd, result):
    await init_db()
    ran = await _turn(monkeypatch, [[_rc(cmd)], [("edit_file", EDIT)]],
                      {"run_code": result})
    assert ran == ["run_code"]                     # the edit never reached the handler


async def test_python_code_in_run_code_is_not_parsed_as_a_read(tmp_env, monkeypatch):
    await init_db()
    ran = await _turn(monkeypatch, [
        [("run_code", '{"code": "print(open(\'src/a.py\').read())"}')],
        [("edit_file", EDIT)]], {"run_code": OK})
    assert ran == ["run_code"]


async def test_a_find_text_a_tool_already_returned_counts_as_read(tmp_env, monkeypatch):
    """The file's text came back from some tool this turn (a grep, a diff): the
    edit goes ahead, and edit_file itself still demands an exact, unique match."""
    await init_db()
    snippet = "    timeout = min(int(timeout_seconds) or DEFAULT_TIMEOUT, MAX_TIMEOUT)"
    edit = ('{"path": "src/a.py", "replace": "y", "find": '
            '"    timeout = min(int(timeout_seconds) or DEFAULT_TIMEOUT, MAX_TIMEOUT)"}')
    ran = await _turn(monkeypatch, [[("web_read", "{}")], [("edit_file", edit)]],
                      {"web_read": f"259:{snippet}\n260:    cwd = x"})
    assert ran == ["web_read", "edit_file"]
    # a short find is too easy to match by accident
    short = '{"path": "src/a.py", "find": "timeout", "replace": "y"}'
    ran = await _turn(monkeypatch, [[("web_read", "{}")], [("edit_file", short)]],
                      {"web_read": f"259:{snippet}"})
    assert ran == ["web_read"]


async def test_nothing_read_is_still_refused_with_the_old_words(tmp_env, monkeypatch):
    await init_db()
    ran = await _turn(monkeypatch, [[("edit_file", EDIT)]])
    assert ran == []


def test_guard_message_keeps_the_read_file_wording():
    loop_mod._files_seen.clear()
    msg = _guard_blind_edit(1, "edit_file", {"path": "src/a.py", "find": "x"})
    assert "haven't read 'src/a.py'" in msg and "read_file on it first" in msg
    assert "run_code" in msg                       # and says a cat counts too


def test_paths_are_compared_normalised():
    loop_mod._files_seen.clear()
    loop_mod._note_seen(7, "./src//a.py")
    assert _guard_blind_edit(7, "edit_file", {"path": "src/a.py", "find": "x"}) is None
    assert _guard_blind_edit(7, "edit_file", {"path": "./src/a.py", "find": "x"}) is None
    assert _guard_blind_edit(7, "edit_file", {"path": "src/b.py", "find": "x"}) is not None


def test_paths_read_by_parse():
    assert _paths_read_by("cat src/a.py") == {"src/a.py"}
    assert _paths_read_by("cat src/a.py; head -3 lib/b.py") == {"src/a.py", "lib/b.py"}
    assert "src/a.py" in _paths_read_by("sed -n 1,5p ./src/a.py")
    assert "src/a.py" in _paths_read_by("cat 'src/a.py' 2>/dev/null")
    assert "src/a.py" not in _paths_read_by("cat src/a.py | wc -l")
    assert "src/a.py" not in _paths_read_by("cat src/a.py > out.txt")
    assert "src/a.py" not in _paths_read_by("cat > src/a.py <<EOF\nhi\nEOF")
    assert "src/a.py" not in _paths_read_by("sed -i s/a/b/ src/a.py")
    assert _paths_read_by("cat 'unclosed") == set()


def test_an_absolute_path_inside_the_projects_dir_is_the_project_path(monkeypatch, tmp_path):
    from backend.config import settings
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    got = _paths_read_by(f"cat {tmp_path}/proj/src/a.py")
    assert "src/a.py" in got
