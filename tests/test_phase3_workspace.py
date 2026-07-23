"""Phase 3 M3: the workspace round-trip. The host pushes a workspace, the guest
edits a file IN the guest (subprocess, stdlib-only guest env) — buffered in the
guest's own .staging overlay — and the host applies the buffer back through
writes.apply_write (secret refusal + advisory diff gate), landing it on the
canonical files. The guest itself still never touches host files directly."""
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from backend.config import settings
from backend.vm import workspace_xfer
from backend.vm.guest_pkg import build_package_tar


def _tar_dir(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=str(p.relative_to(root)))
    return buf.getvalue()


async def test_workspace_roundtrip_edit_in_guest_reconciled(tmp_env):
    # host: a project with one canonical file
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# demo\n")
    (proj / "hello.txt").write_text("original")
    merged = workspace_xfer.build_merged_tar("demo")

    # guest: unpack the package + the merged workspace, run write_file locally
    gdir = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(gdir, filter="data")
    gproj = Path(gdir) / "projects" / "demo"
    gproj.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(merged), mode="r:gz") as t:
        t.extractall(gproj, filter="data")

    script = (
        "import asyncio\n"
        "from backend.agent.tools import registry, toolctx\n"
        "toolctx.set_active('demo')\n"
        "print(asyncio.run(registry.dispatch('write_file', "
        "{'path': 'notes/new.txt', 'content': 'edited from the guest'})))\n")
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=gdir,
                       env={"PYTHONPATH": gdir, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True)
    assert r.returncode == 0 and "wrote notes/new.txt" in r.stdout, r.stdout + r.stderr

    # the guest buffered into its OWN .staging (guest canonical copy untouched)
    gstaged = gproj / ".staging" / "notes" / "new.txt"
    assert gstaged.is_file() and gstaged.read_text() == "edited from the guest"

    # host: apply the guest's buffer through writes.apply_write -> canonical
    result = await workspace_xfer.apply_guest_writes("demo", _tar_dir(gproj / ".staging"))
    assert "notes/new.txt" in result["applied"]
    assert result["secret_files"] == {}
    landed = settings.projects_dir / "demo" / "notes" / "new.txt"
    assert landed.is_file() and landed.read_text() == "edited from the guest"
    # untouched files stay untouched
    assert (settings.projects_dir / "demo" / "hello.txt").read_text() == "original"
