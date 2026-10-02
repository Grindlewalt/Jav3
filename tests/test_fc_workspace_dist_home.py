"""FC: what crosses the host/guest line for dist/ and for HOME-style dot dirs.

benchmark-game, 2026-10-01: `dist/` was skipped going INTO the guest, so every
turn saw no build the agent had made before; and a Chromium or pip run from
run_code (HOME is the project) would write .config / .local / .pki into the
project. dist/ now ships (up to a cap, with a note past it); those dirs are
dropped coming out unless the project has its own.
"""
import io
import subprocess
import tarfile

import pytest

from backend.config import settings
from backend.db import init_db
from backend.vm import workspace_xfer


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _names(tar_bytes: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
        return {m.name for m in t.getmembers()}


def _proj(files: dict[str, str], slug="fc"):
    d = settings.projects_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def _git(d, *args):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


@pytest.fixture
async def env(tmp_env):
    await init_db()
    return tmp_env


# --- dist/ goes in -----------------------------------------------------------

def test_dist_ships_into_the_guest_but_dependencies_do_not(env):
    _proj({"main.py": "x\n", "dist/game.html": "<html></html>", "web/dist/app.js": "1",
           "node_modules/x/i.js": "junk", "node_modules/x/dist/y.js": "junk"})
    tar, note = workspace_xfer.build_workspace("fc")
    got = _names(tar)
    assert {"main.py", "dist/game.html", "web/dist/app.js"} <= got
    assert not any(n.startswith("node_modules") for n in got)
    assert note == "" and workspace_xfer.workspace_note("fc") == ""
    assert _names(workspace_xfer.build_merged_tar("fc")) == got


def test_a_dist_over_the_cap_stays_out_and_the_note_says_so(env, monkeypatch):
    monkeypatch.setattr(workspace_xfer, "DIST_MAX_BYTES", 100)
    _proj({"main.py": "x\n", "dist/a.bin": "a" * 80, "dist/b.bin": "b" * 80})
    tar, note = workspace_xfer.build_workspace("fc")
    assert _names(tar) == {"main.py"}
    assert "dist/" in note and "not copied in" in note
    assert workspace_xfer.workspace_note("fc") == note
    # a dist that shrank back under the cap ships again and the note clears
    (settings.projects_dir / "fc" / "dist" / "b.bin").unlink()
    tar, note = workspace_xfer.build_workspace("fc")
    assert "dist/a.bin" in _names(tar) and note == ""
    assert workspace_xfer.workspace_note("fc") == ""


def test_a_built_file_holding_a_stored_secret_value_does_not_ship(env):
    from backend import secrets as secrets_mod
    secrets_mod.save({"FC_KEY": "sk-fc-0123456789abcdef"})
    _proj({"dist/bundle.js": "var k='sk-fc-0123456789abcdef'", "dist/ok.js": "1"})
    got = _names(workspace_xfer.build_merged_tar("fc"))
    assert "dist/ok.js" in got and "dist/bundle.js" not in got


async def test_a_guest_deletion_of_a_shipped_dist_file_is_honoured(env):
    d = _proj({"dist/old.js": "old\n", "keep.txt": "k\n"})
    workspace_xfer.build_merged_tar("fc")
    res = await workspace_xfer.apply_guest_writes(
        "fc", _tar({".staging/deleted.json": '["dist/old.js"]', "new.txt": "n\n"}))
    assert res["deleted"] == ["dist/old.js"]
    assert not (d / "dist" / "old.js").exists() and (d / "keep.txt").exists()


def test_a_slug_never_built_has_no_note(env):
    _proj({"a.txt": "a\n"}, slug="one")
    assert workspace_xfer.workspace_note("one") == ""
    assert workspace_xfer.workspace_note("never-built") == ""


# --- HOME-style dot dirs stay out --------------------------------------------

async def test_tool_run_droppings_under_home_dot_dirs_are_dropped_quietly(env):
    d = _proj({"a.txt": "a\n"})
    res = await workspace_xfer.apply_guest_writes("fc", _tar({
        ".config/chromium/Default/Preferences": "{}", ".local/lib/python3/x.py": "x",
        ".pki/nssdb/cert9.db": "x", ".cache/pip/http/0": "x", "app/.config/y": "y",
        "ok.txt": "ok\n"}))
    assert res["applied"] == ["ok.txt"]
    assert not res["refused"] and not res["failed"]
    for junk in (".config", ".local", ".pki", ".cache", "app"):
        assert not (d / junk).exists()
    assert workspace_xfer.describe_unapplied(res) == ""


async def test_a_dot_dir_the_project_tracks_in_git_keeps_working(env):
    d = _proj({"a.txt": "a\n", ".config/app.toml": "v = 1\n"})
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    res = await workspace_xfer.apply_guest_writes("fc", _tar({
        ".config/app.toml": "v = 2\n", ".config/extra.toml": "w = 1\n",
        ".local/share/junk": "x"}))
    assert sorted(res["applied"]) == [".config/app.toml", ".config/extra.toml"]
    assert (d / ".config" / "app.toml").read_text() == "v = 2\n"
    assert not (d / ".local").exists()


async def test_a_dot_dir_that_went_into_the_guest_this_turn_is_the_projects_own(env):
    # not a git repo, but the host holds .config/app.toml and shipped it in
    d = _proj({"a.txt": "a\n", ".config/app.toml": "v = 1\n"})
    assert ".config/app.toml" in _names(workspace_xfer.build_merged_tar("fc"))
    res = await workspace_xfer.apply_guest_writes("fc", _tar({
        ".config/app.toml": "v = 2\n", ".pki/x": "x"}))
    assert res["applied"] == [".config/app.toml"]
    assert (d / ".config" / "app.toml").read_text() == "v = 2\n"
    assert not (d / ".pki").exists()


async def test_a_git_project_without_the_dir_tracked_still_drops_it(env):
    d = _proj({"a.txt": "a\n"})
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    res = await workspace_xfer.apply_guest_writes("fc", _tar({".cache/x": "x", "b.txt": "b\n"}))
    assert res["applied"] == ["b.txt"] and not (d / ".cache").exists()


def test_a_file_named_like_a_home_dir_is_not_one():
    assert workspace_xfer._home_dir(".config") is None
    assert workspace_xfer._home_dir("src/.cache") is None
    assert workspace_xfer._home_dir("src/.cache/x") == "src/.cache"
    assert workspace_xfer._home_dir("a.txt") is None
