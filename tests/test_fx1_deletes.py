"""FX1 / BUILD-01: deletes and renames made in the guest reach the project.

An agent's `mv` and `rm` act on the guest's copy of the tree, while write_file
buffers into .staging, so a removed file was never in the buffer and never left
the project; run_code, the agent and its final message all said it was gone
(conv 637: rename tests/test_csv2md.py, delete todo.md). The guest now lists what
it removed in the buffer; the host applies each through its own gates and only
for a file it shipped and that has not changed since.
"""
import base64
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from backend import alwaysloaded, secrets as secrets_mod, taintpaths
from backend.config import settings
from backend.db import get_db, init_db
from backend.vm import broker, workspace_xfer
from backend.vm.guest_pkg import build_package_tar

DEL = workspace_xfer.DELETED_MEMBER


def _tar(files: dict[str, str], deleted: list[str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        if deleted is not None:
            data = json.dumps(deleted).encode()
            ti = tarfile.TarInfo(DEL)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _proj(files: dict[str, str], slug="del", *, ship=True) -> Path:
    d = settings.projects_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    if ship:
        workspace_xfer.build_merged_tar(slug)       # what the guest was given
    return d


@pytest.fixture
async def env(tmp_env):
    await init_db()
    workspace_xfer._shipped.clear()
    return tmp_env


async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT summary, detail FROM security_events WHERE kind = ? "
                              "ORDER BY id", (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


# --- the host applies what the guest reports -------------------------------------

async def test_a_rename_and_a_delete_reach_the_project(env):
    """conv 637 turn 3, replayed: rename tests/test_csv2md.py to tests/test_convert.py
    and delete todo.md. The project ended with both names and todo.md."""
    d = _proj({"tests/test_csv2md.py": "def test_x():\n    pass\n", "todo.md": "- a\n",
               "keep.txt": "k\n"})
    res = await workspace_xfer.apply_guest_writes("del", _tar(
        {"tests/test_convert.py": "def test_x():\n    pass\n"},
        deleted=["tests/test_csv2md.py", "todo.md"]))
    assert res["applied"] == ["tests/test_convert.py"]
    assert sorted(res["deleted"]) == ["tests/test_csv2md.py", "todo.md"]
    assert res["not_deleted"] == {}
    assert (d / "tests/test_convert.py").is_file()
    assert not (d / "tests/test_csv2md.py").exists() and not (d / "todo.md").exists()
    assert (d / "keep.txt").read_text() == "k\n"
    assert workspace_xfer.describe_unapplied(res) == ""      # nothing to apologise for


async def test_an_emptied_folder_goes_with_its_last_file(env):
    d = _proj({"old/deep/a.txt": "a\n", "keep.txt": "k\n"})
    await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["old/deep/a.txt"]))
    assert not (d / "old").exists() and (d / "keep.txt").exists()


async def test_the_same_report_twice_is_harmless(env):
    """A mid-turn flush and the turn-end buffer both carry it."""
    d = _proj({"todo.md": "- a\n"})
    first = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["todo.md"]))
    second = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["todo.md"]))
    assert first["deleted"] == ["todo.md"]
    assert second["deleted"] == [] and second["not_deleted"] == {}
    assert not (d / "todo.md").exists()


async def test_a_file_the_guest_wrote_mid_turn_can_be_deleted_later(env):
    """Applied writes join what the host knows, so a flushed file removed afterwards goes."""
    d = _proj({"a.txt": "a\n"})
    await workspace_xfer.apply_guest_writes("del", _tar({"tmp.txt": "scratch\n"}))
    assert (d / "tmp.txt").is_file()
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["tmp.txt"]))
    assert res["deleted"] == ["tmp.txt"] and not (d / "tmp.txt").exists()


# --- ...and refuses what it should -----------------------------------------------

async def test_a_lying_guest_cannot_delete_what_it_was_never_shown(env):
    d = _proj({"a.txt": "a\n"})
    (d / "later.txt").write_text("added on the host after the copy was made\n")
    (d / ".context.json").write_text("{}")
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=[
        "later.txt", ".context.json", ".git/config", "../../etc/passwd", "/etc/hosts",
        "node_modules/x.js", 7, None]))
    assert res["deleted"] == []
    assert set(res["not_deleted"]) == {"later.txt", "../../etc/passwd", "/etc/hosts"}
    assert (d / "later.txt").is_file() and (d / ".context.json").is_file()


async def test_a_file_changed_on_the_host_since_the_copy_is_kept(env):
    """The operator edited it while the turn ran: their newer text is not the
    agent's to delete."""
    d = _proj({"notes.md": "as shipped\n"})
    (d / "notes.md").write_text("the operator's edit\n")
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["notes.md"]))
    assert res["deleted"] == []
    assert "changed in the project" in res["not_deleted"]["notes.md"]
    assert (d / "notes.md").read_text() == "the operator's edit\n"
    assert "notes.md" in workspace_xfer.describe_unapplied(res)


async def test_the_gitignore_is_never_deleted(env):
    d = _proj({".gitignore": ".staging/\n.workspace.json\n.context.json\ndata/\n", "a.txt": "a\n"})
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=[".gitignore"]))
    assert res["deleted"] == [] and ".gitignore" in res["not_deleted"]
    assert (d / ".gitignore").is_file()


async def test_a_rename_whose_new_half_is_refused_keeps_the_old_half(env):
    """The bytes must not vanish from both names."""
    secrets_mod.save({"API_KEY": "sk_live_abcdef123456"})
    d = _proj({"config.js": "const k = 'sk_live_abcdef123456';\n"}, ship=False)
    # (a host file that already holds a stored value: shipped by a legacy path)
    workspace_xfer._shipped["del"] = {"config.js": workspace_xfer._sha((d / "config.js").read_bytes())}
    res = await workspace_xfer.apply_guest_writes("del", _tar(
        {"settings.js": "const k = 'sk_live_abcdef123456';\n"}, deleted=["config.js"]))
    assert res["secret_files"] == {"settings.js": ["API_KEY"]}
    assert res["deleted"] == [] and (d / "config.js").is_file()
    assert "config.js" in res["not_deleted"]


async def test_a_tainted_turn_cannot_delete_a_file_that_rides_every_prompt(env):
    d = _proj({"project.md": "# demo\n", "notes.md": "n\n"})
    broker.register_turn(broker.TurnEnvelope(op_id="op-t", web_session="ws", active_project="del"))
    try:
        broker.mark_tainted("op-t")
        res = await workspace_xfer.apply_guest_writes(
            "del", _tar({}, deleted=["project.md", "notes.md"]), "op-t")
    finally:
        broker.release_turn("op-t")
    assert (d / "project.md").is_file() and "project.md" in res["not_deleted"]
    assert res["deleted"] == ["notes.md"]                     # an ordinary file goes


async def test_a_clean_turn_may_delete_project_md_like_any_other_change(env):
    d = _proj({"project.md": "# demo\n"})
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["project.md"]))
    assert res["deleted"] == ["project.md"] and not (d / "project.md").exists()


async def test_a_ticked_context_file_is_guarded_the_same_way(env):
    d = _proj({"notes/spec.md": "spec\n"})
    alwaysloaded.set_selection("del", ["notes/spec.md"])
    broker.register_turn(broker.TurnEnvelope(op_id="op-t", web_session="ws", active_project="del"))
    try:
        broker.mark_tainted("op-t")
        res = await workspace_xfer.apply_guest_writes(
            "del", _tar({}, deleted=["notes/spec.md"]), "op-t")
    finally:
        broker.release_turn("op-t")
    assert res["deleted"] == [] and (d / "notes/spec.md").is_file()


async def test_the_diff_gate_scans_a_deleted_file_like_a_rewrite_to_nothing(env):
    """Deleting a test file drops its assertions: the advisory tripwire fires, the
    delete still happens (advisory, as for a write)."""
    d = _proj({"tests/test_a.py": "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n"})
    await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=["tests/test_a.py"]))
    assert not (d / "tests/test_a.py").exists()
    ev = await _events("write_flag")
    assert any("assertion_removed" in e["summary"] and "tests/test_a.py" in e["summary"]
               for e in ev)


async def test_too_long_a_list_is_cut_not_followed(env):
    d = _proj({"a.txt": "a\n"})
    many = [f"ghost{i}.txt" for i in range(workspace_xfer.MAX_DELETIONS + 50)]
    res = await workspace_xfer.apply_guest_writes("del", _tar({}, deleted=many + ["a.txt"]))
    assert res["deleted"] == [] and (d / "a.txt").is_file()    # the real one fell past the cap


async def test_a_list_that_is_not_json_is_ignored(env):
    d = _proj({"a.txt": "a\n"})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        ti = tarfile.TarInfo(DEL)
        ti.size = 4
        t.addfile(ti, io.BytesIO(b"nope"))
    res = await workspace_xfer.apply_guest_writes("del", buf.getvalue())
    assert res["deleted"] == [] and (d / "a.txt").is_file()
    assert res["refused"] == {}                                # not reported as a file


# --- taint follows a rename ------------------------------------------------------

async def test_a_tainted_file_stays_tainted_under_its_new_name(env):
    d = _proj({"page.html": "<p>fetched from the web</p>\n", "b.txt": "b\n"})
    taintpaths.record("del", ["page.html"], True)              # a tainted turn wrote it
    await workspace_xfer.apply_guest_writes("del", _tar(
        {"saved/page.html": "<p>fetched from the web</p>\n"}, deleted=["page.html"]))
    ledger = taintpaths.paths("del")
    assert "saved/page.html" in ledger and "page.html" not in ledger
    assert not (d / "page.html").exists()


# --- the real guest code ---------------------------------------------------------

_SHIM = ("import socket\n"
         "socket.AF_VSOCK = getattr(socket, 'AF_VSOCK', 40)\n"
         "socket.VMADDR_CID_HOST = getattr(socket, 'VMADDR_CID_HOST', 2)\n"
         "socket.VMADDR_CID_ANY = getattr(socket, 'VMADDR_CID_ANY', 0xFFFFFFFF)\n")


def _guest_run(gdir: str, body: str) -> str:
    """Run `body` inside the pushed guest package (the guest's own run-turn
    server module), as the guest would: stdlib only."""
    r = subprocess.run([sys.executable, "-S", "-c", _SHIM + body], cwd=gdir,
                       env={"PYTHONPATH": gdir, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


async def test_the_guests_own_pack_lists_what_the_shell_removed(env, tmp_path):
    """The whole path with the guest's real server code: unpack the host's copy,
    `rm` and `mv` in the tree, stage one edit, pack; the host applies the result."""
    d = _proj({"tests/test_csv2md.py": "T\n", "todo.md": "- a\n", "keep.txt": "k\n",
               "edit.txt": "old\n", ".gitignore": ".staging/\n"})
    tar_b64 = base64.b64encode(workspace_xfer.build_merged_tar("del")).decode()
    gdir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(gdir, filter="data")
    out = _guest_run(gdir, (
        "import base64, os, shutil\n"
        "from backend import server\n"
        f"server._unpack_workspace('del', {tar_b64!r})\n"
        "root = server.guest_config.settings.projects_dir / 'del'\n"
        "shutil.move(root / 'tests/test_csv2md.py', root / 'tests/test_convert.py')\n"
        "os.remove(root / 'todo.md')\n"
        "(root / '.staging').mkdir()\n"
        "(root / '.staging/edit.txt').write_text('new\\n')\n"
        "(root / '.staging/tests').mkdir()\n"
        "(root / '.staging/tests/test_convert.py').write_text('T\\n')\n"
        "print(server._pack_staging('del'))\n"))
    tar_bytes = base64.b64decode(out.strip())
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as t:
        names = {m.name for m in t.getmembers()}
        listed = json.loads(t.extractfile(DEL).read())
    assert names == {"edit.txt", "tests/test_convert.py", DEL}
    assert sorted(listed) == ["tests/test_csv2md.py", "todo.md"]
    res = await workspace_xfer.apply_guest_writes("del", tar_bytes)
    assert sorted(res["deleted"]) == ["tests/test_csv2md.py", "todo.md"]
    assert (d / "tests/test_convert.py").is_file() and not (d / "todo.md").exists()
    assert (d / "edit.txt").read_text() == "new\n" and (d / "keep.txt").is_file()


async def test_the_guests_pack_does_not_call_a_staged_file_a_deletion(env):
    """A shipped file removed from the tree but rewritten through write_file is a
    write, not a deletion; and an untouched workspace reports nothing."""
    _proj({"a.txt": "a\n", "b.txt": "b\n"})
    tar_b64 = base64.b64encode(workspace_xfer.build_merged_tar("del")).decode()
    gdir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(gdir, filter="data")
    out = _guest_run(gdir, (
        "import os\n"
        "from backend import server\n"
        f"server._unpack_workspace('del', {tar_b64!r})\n"
        "root = server.guest_config.settings.projects_dir / 'del'\n"
        "print(len(server._pack_staging('del')) > 0)\n"          # untouched
        "os.remove(root / 'a.txt')\n"
        "(root / '.staging').mkdir()\n"
        "(root / '.staging/a.txt').write_text('rewritten\\n')\n"
        "import base64, io, tarfile, json\n"
        "t = tarfile.open(fileobj=io.BytesIO(base64.b64decode(server._pack_staging('del'))))\n"
        "print(sorted(t.getnames()))\n"))
    first, second = out.strip().splitlines()
    assert first == "True"
    assert second == "['a.txt']"                                # a write; no deletion list
