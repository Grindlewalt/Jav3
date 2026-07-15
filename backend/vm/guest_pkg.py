"""Assemble the guest runtime package tarball from repo sources.

The guest is pushed this package at boot (the gateway's `get_guest_package` op),
so guest code == host code with no image rebuild per change. The package is the
checked-in `guest/jarvis_guest/` tree (shims + the run-turn server) plus a live
copy of `backend/agent/loop.py` dropped in at `jarvis_guest/agent/loop.py` — the
ReAct engine runs in the guest verbatim, its five imports resolving to the shims.
"""
import io
import tarfile

from ..config import settings


def _guest_src():
    return settings.base_dir / "guest" / "jarvis_guest"


def build_package_tar() -> bytes:
    src = _guest_src()
    loop_py = settings.base_dir / "backend" / "agent" / "loop.py"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(src.rglob("*.py")):
            tar.add(p, arcname=f"jarvis_guest/{p.relative_to(src)}")
        data = loop_py.read_bytes()          # the engine, copied live from backend
        ti = tarfile.TarInfo("jarvis_guest/agent/loop.py")
        ti.size = len(data)
        ti.mode = 0o644
        tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()
