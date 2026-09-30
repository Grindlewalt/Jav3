"""FX1 / BUILD-02: the guest gets the project's dotfiles.

The guest used to see a project with no .gitignore (list_tree skipped every
dotfile), so an agent that wrote one replaced the real one and dropped the
harness's own ignore lines (.staging/, .workspace.json, .context.json, data/).
Dotfiles now ship into the guest; credentials do not; and the host keeps the
harness lines in the root .gitignore whatever the guest sends back.
"""
import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

from backend import gitgate, secrets as secrets_mod
from backend.config import settings
from backend.db import init_db
from backend.fsutil import list_tree
from backend.vm import workspace_xfer

REPO = Path(__file__).resolve().parent.parent
HARNESS = [ln for ln in gitgate.GITIGNORE.splitlines() if ln]


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _shipped(slug="dot") -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(workspace_xfer.build_merged_tar(slug)), mode="r:gz") as t:
        return {m.name for m in t.getmembers()}


def _proj(files: dict[str, str], slug="dot") -> Path:
    d = settings.projects_dir / slug
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


@pytest.fixture
async def env(tmp_env):
    await init_db()
    return tmp_env


def test_dotfiles_ship_into_the_guest(env):
    _proj({"main.py": "print(1)\n", ".gitignore": gitgate.GITIGNORE,
           ".eslintrc": "{}\n", ".github/workflows/ci.yml": "on: push\n",
           ".env.example": "KEY=\n", "src/.prettierrc": "{}\n"})
    got = _shipped()
    assert {"main.py", ".gitignore", ".eslintrc", ".github/workflows/ci.yml",
            ".env.example", "src/.prettierrc"} <= got


def test_the_harness_files_and_credentials_stay_out(env):
    _proj({"main.py": "x\n", ".git/config": "[core]\n", ".staging/a.txt": "a\n",
           ".workspace.json": "{}", ".context.json": "{}", ".env": "K=secret\n",
           ".env.production": "K=secret\n", ".env.local": "K=secret\n",
           ".ssh/id_rsa": "key\n", ".netrc": "machine x\n", ".aws/credentials": "k\n",
           ".cache/pip/x": "x\n", ".mypy_cache/x": "x\n", "node_modules/pkg/i.js": "x\n",
           ".venv/bin/python": "x\n", "__pycache__/m.pyc": "x\n"})
    assert _shipped() == {"main.py"}


def test_a_dotfile_holding_a_stored_secret_value_does_not_ship(env):
    secrets_mod.save({"NPM_TOKEN": "npm_abcdef123456"})
    _proj({".npmrc": "//registry.npmjs.org/:_authToken=npm_abcdef123456\n",
           ".prettierrc": "{}\n", "main.py": "x\n"})
    got = _shipped()
    assert ".npmrc" not in got and ".prettierrc" in got and "main.py" in got


def test_the_operators_listings_still_hide_dotfiles_and_the_guests_show_them(env):
    d = _proj({"main.py": "x\n", ".gitignore": "a\n", ".github/w.yml": "x\n",
               ".staging/s.txt": "s\n", ".git/HEAD": "h\n", ".workspace.json": "{}",
               ".cache/c": "c\n"})
    assert {e["path"] for e in list_tree(d)} == {"main.py"}                 # unchanged
    assert {e["path"] for e in list_tree(d, dotfiles=True)} == {
        "main.py", ".gitignore", ".github/w.yml"}
    spec = importlib.util.spec_from_file_location("guest_fsutil", REPO / "guest/backend/fsutil.py")
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    assert {e["path"] for e in guest.list_tree(d)} == {"main.py", ".gitignore", ".github/w.yml"}
    assert {e["path"] for e in guest.list_tree(d, dotfiles=False)} == {"main.py"}


async def test_an_agent_written_gitignore_keeps_the_harness_lines(env):
    """conv 637: `cat > .gitignore` left only __pycache__/ *.pyc .pytest_cache/, and a
    later `git add -A` could stage data/, .workspace.json and .context.json."""
    d = _proj({".gitignore": gitgate.GITIGNORE})
    res = await workspace_xfer.apply_guest_writes(
        "dot", _tar({".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n"}))
    assert res["applied"] == [".gitignore"]
    lines = (d / ".gitignore").read_text().splitlines()
    assert lines[:3] == ["__pycache__/", "*.pyc", ".pytest_cache/"]         # the agent's own
    assert all(h in lines for h in HARNESS)                                 # and ours


async def test_a_gitignore_without_a_trailing_newline_is_still_well_formed(env):
    d = _proj({"a.txt": "a\n"})
    await workspace_xfer.apply_guest_writes("dot", _tar({".gitignore": "*.log"}))
    assert (d / ".gitignore").read_text().splitlines() == ["*.log", *HARNESS]


async def test_a_gitignore_that_already_has_the_lines_is_written_as_is(env):
    d = _proj({"a.txt": "a\n"})
    text = "# mine\n" + gitgate.GITIGNORE + "*.log\n"
    await workspace_xfer.apply_guest_writes("dot", _tar({".gitignore": text}))
    assert (d / ".gitignore").read_text() == text


async def test_only_the_root_gitignore_is_merged(env):
    d = _proj({"a.txt": "a\n"})
    await workspace_xfer.apply_guest_writes("dot", _tar({"sub/.gitignore": "*.tmp\n"}))
    assert (d / "sub/.gitignore").read_text() == "*.tmp\n"


async def test_writes_to_harness_paths_are_refused_and_generated_junk_is_dropped_quietly(env):
    d = _proj({"a.txt": "a\n"})
    res = await workspace_xfer.apply_guest_writes(
        "dot", _tar({".context.json": "{}", ".git/config": "x", "__pycache__/m.pyc": "x",
                     ".pytest_cache/v": "x", "ok.txt": "ok\n"}))
    assert res["applied"] == ["ok.txt"]
    assert set(res["refused"]) == {".context.json", ".git/config"}
    assert not (d / "__pycache__").exists() and not (d / ".context.json").exists()
    note = workspace_xfer.describe_unapplied(res)
    assert ".context.json" in note and "pycache" not in note
