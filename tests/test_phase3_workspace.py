"""Phase 3 M3: the workspace round-trip. The host pushes a merged workspace, the
guest edits a file IN the guest (subprocess, stdlib-only guest env), and the host
reconciles the guest's staged edit back through stage_write with a secret scan —
the operator's canonical files are never touched; approval stays host-side."""
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


def test_workspace_roundtrip_edit_in_guest_reconciled(tmp_env):
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
    assert r.returncode == 0 and "staged write" in r.stdout, r.stdout + r.stderr

    # the guest staged into its OWN .staging (canonical untouched)
    gstaged = gproj / ".staging" / "notes" / "new.txt"
    assert gstaged.is_file() and gstaged.read_text() == "edited from the guest"

    # host: reconcile the guest's .staging back through stage_write
    result = workspace_xfer.reconcile_staged("demo", _tar_dir(gproj / ".staging"))
    assert "notes/new.txt" in result["staged"]
    assert result["secret_files"] == {}
    host_staged = settings.projects_dir / "demo" / ".staging" / "notes" / "new.txt"
    assert host_staged.is_file() and host_staged.read_text() == "edited from the guest"
    # the operator's canonical file was never touched by the guest
    assert (settings.projects_dir / "demo" / "hello.txt").read_text() == "original"
