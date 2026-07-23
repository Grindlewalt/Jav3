"""Phase 3 M1: the guest runtime package assembles from repo sources and imports
in a stdlib-only subprocess — proving loop.py runs in the guest via the shim
import-mirroring, with no host-only import (aiosqlite/httpx/backend/…) leaking in.
This is the regression guard for "loop.py stays guest-copyable"."""
import io
import os
import subprocess
import sys
import tarfile
import tempfile

from backend.vm.guest_pkg import build_package_tar


def test_guest_package_assembles_and_imports_stdlib_only():
    data = build_package_tar()
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        names = {m.name for m in t.getmembers()}
        t.extractall(d, filter="data")
    assert "backend/agent/loop.py" in names
    assert "backend/server.py" in names
    assert "backend/writes.py" in names           # the guest write-buffer shim
    assert "tools/read_file/handler.py" in names   # a clean in-guest handler
    # -S: no site-packages, so third-party libs are OFF the path — if loop.py or a
    # copied module (codeindex) or shim imported anything but stdlib + the guest
    # backend, these imports fail.
    r = subprocess.run(
        [sys.executable, "-S", "-c",
         "import backend.server; from backend.agent.loop import run_turn; "
         "import backend.writes, backend.codeindex, backend.fsutil; print('GUEST-OK')"],
        cwd=d, env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True)
    assert r.returncode == 0 and "GUEST-OK" in r.stdout, r.stderr


def _extract_pkg():
    d = tempfile.mkdtemp()
    with tarfile.open(fileobj=io.BytesIO(build_package_tar()), mode="r:gz") as t:
        t.extractall(d, filter="data")
    return d


def test_in_guest_tool_runs_locally_against_pushed_workspace():
    """M3: a clean tool (read_file) loads its handler from the pushed package and
    runs IN the guest against a pushed workspace — its `from backend.X` imports
    resolve to the guest shims, no host, no vsock, stdlib only."""
    d = _extract_pkg()
    proj = os.path.join(d, "projects", "demo")
    os.makedirs(proj)
    with open(os.path.join(proj, "project.md"), "w") as f:
        f.write("# demo\n")
    with open(os.path.join(proj, "hello.txt"), "w") as f:
        f.write("hi from inside the guest")
    script = (
        "import asyncio\n"
        "from backend.agent.tools import registry, toolctx\n"
        "toolctx.set_active('demo')\n"
        "registry.set_registry([], [])\n"
        "print('OUT:' + asyncio.run(registry.dispatch('read_file', {'path': 'hello.txt'})))\n")
    r = subprocess.run([sys.executable, "-S", "-c", script], cwd=d,
                       env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "hi from inside the guest" in r.stdout, r.stdout + r.stderr
