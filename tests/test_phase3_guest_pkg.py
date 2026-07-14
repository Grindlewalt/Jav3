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
    assert "jarvis_guest/agent/loop.py" in names
    assert "jarvis_guest/server.py" in names
    # -S: no site-packages, so third-party libs are OFF the path — if loop.py or a
    # shim imported anything but stdlib + jarvis_guest, this import fails.
    r = subprocess.run(
        [sys.executable, "-S", "-c",
         "import jarvis_guest.server; "
         "from jarvis_guest.agent.loop import run_turn; print('GUEST-OK')"],
        cwd=d, env={"PYTHONPATH": d, "PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True)
    assert r.returncode == 0 and "GUEST-OK" in r.stdout, r.stderr
