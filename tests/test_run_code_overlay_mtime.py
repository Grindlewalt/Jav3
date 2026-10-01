"""FX1 note: a file staged with write_file and then removed with `rm` inside
run_code came back at turn end (the staged copy was shipped as a write).
BUILD-14: files unpack into the guest with their real mtime, not 1970."""
import importlib.util
import io
import os
import tarfile

import pytest

from backend.agent.tools import toolctx
from backend.config import settings
from backend.vm import workspace_xfer

ROOT = settings.base_dir


def _handler():
    spec = importlib.util.spec_from_file_location("rc_h", ROOT / "tools/run_code/handler.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    return h


@pytest.fixture
def guest_dir(tmp_env, monkeypatch):
    monkeypatch.setattr(settings, "in_guest", True, raising=False)

    async def slug():
        return "demo"
    monkeypatch.setattr(toolctx, "active_slug", slug)
    d = settings.projects_dir / "demo"
    (d / ".staging").mkdir(parents=True)
    return d


async def test_a_staged_file_removed_by_the_run_does_not_come_back(guest_dir):
    d = guest_dir
    (d / "old.txt").write_text("canonical\n")              # shipped, then edited via edit_file
    (d / ".staging" / "old.txt").write_text("edited\n")
    (d / ".staging" / "new.txt").write_text("fresh\n")      # written with write_file
    (d / ".staging" / "sub").mkdir()
    (d / ".staging" / "sub" / "keep.txt").write_text("kept\n")
    out = await _handler().run(command="rm old.txt new.txt")
    assert out.startswith("exit 0"), out
    assert not (d / ".staging" / "old.txt").exists()
    assert not (d / ".staging" / "new.txt").exists()
    assert (d / ".staging" / "sub" / "keep.txt").read_text() == "kept\n", "untouched staged files stay"
    assert (d / "sub" / "keep.txt").exists()


async def test_a_staged_file_the_run_edits_keeps_the_runs_version(guest_dir):
    d = guest_dir
    (d / ".staging" / "a.txt").write_text("staged\n")
    out = await _handler().run(command="echo ran >> a.txt")
    assert out.startswith("exit 0"), out
    # run_code's capture writes the changed file back through the writes shim
    assert (d / "a.txt").read_text() == "staged\nran\n"
    assert (d / ".staging" / "a.txt").exists()


def test_files_ship_into_the_guest_with_their_real_mtime(tmp_env):
    d = settings.projects_dir / "demo"
    d.mkdir(parents=True)
    f = d / "a.py"
    f.write_text("print(1)\n")
    os.utime(f, (1_700_000_000, 1_700_000_000))
    with tarfile.open(fileobj=io.BytesIO(workspace_xfer.build_merged_tar("demo")), mode="r:gz") as t:
        assert t.getmember("a.py").mtime == 1_700_000_000
