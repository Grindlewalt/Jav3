"""A tool name that isn't a plain tools/ folder name never loads a handler.

broker_dispatch forwards a guest's tool name verbatim, and pathlib makes
tools_dir / "/abs" == "/abs", so before the fix an absolute path or a `..`
name exec'd any handler.py on the host (A0 hunt, 2026-09-29)."""
import asyncio

from backend.agent.tools import registry
from backend.config import settings


def _evil(tmp_path):
    d = tmp_path / "evil"
    d.mkdir()
    marker = tmp_path / "ran"
    (d / "TOOL.md").write_text("---\nname: evil\ndescription: x\n---\n")
    (d / "handler.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n"
        "async def run(**kw):\n    return 'evil handler ran on host'\n")
    return d, marker


def test_absolute_path_name_is_refused(tmp_path):
    d, marker = _evil(tmp_path)
    out = asyncio.run(registry.dispatch(str(d), {}))
    assert "evil handler ran" not in out
    assert not marker.exists()


def test_dotdot_name_is_refused(tmp_path):
    d, marker = _evil(tmp_path)
    rel = "/".join([".."] * len(settings.tools_dir.resolve().parts)) + str(d)
    out = asyncio.run(registry.dispatch(rel, {}))
    assert "evil handler ran" not in out
    assert not marker.exists()


def test_odd_names_have_no_handler_path():
    for name in ("", ".", "..", "a/b", "a\\b", "x" * 65, "-lead", None, 3):
        assert registry._handler_path(name) is None


def test_real_tool_still_loads():
    assert registry._handler_path("read_file") is not None
    assert registry._load_dynamic("read_file") is not None
