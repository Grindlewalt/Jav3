"""Assemble the guest runtime package tarball from repo sources.

The guest is pushed this package at boot (the gateway's `get_guest_package` op),
so guest code == host code with no image rebuild per change. The package is a
minimal `backend/` tree (named `backend` so the tools' absolute `from backend.X`
imports AND loop.py's relative imports both resolve to the guest shims): the
checked-in `guest/backend/` (shims + run-turn server) plus live copies of the
host modules that run verbatim in the guest (loop.py, codeindex.py, the todo
helpers) and the clean in-guest tool handlers. The guest's own writes.py shim
(checked in under guest/backend/) buffers file writes for turn-end reconcile.
"""
import io
import tarfile

from ..config import settings

# host modules copied VERBATIM into the guest backend (pure — operate on the
# pushed workspace; no host state). arcname -> repo path.
_COPY_MODULES = {
    "backend/agent/loop.py": "backend/agent/loop.py",
    "backend/codeindex.py": "backend/codeindex.py",
    "backend/agent/tools/todostore.py": "backend/agent/tools/todostore.py",
}

# clean tools that run IN the guest (against the pushed workspace); their handler
# is loaded locally. Everything else brokers to the host. run_code lives here and
# ONLY here — code execution exists nowhere on the host.
IN_GUEST_TOOLS = ("read_file", "list_files", "search_codebase", "crawl_codebase",
                  "write_file", "edit_file", "dashboard", "todo_update",
                  "run_code")


def _guest_src():
    return settings.base_dir / "guest" / "backend"


def build_package_tar() -> bytes:
    src = _guest_src()
    base = settings.base_dir
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(src.rglob("*.py")):          # checked-in shims + server
            tar.add(p, arcname=f"backend/{p.relative_to(src)}")
        for arcname, relpath in _COPY_MODULES.items():   # verbatim host modules
            _add_bytes(tar, arcname, (base / relpath).read_bytes())
        for name in IN_GUEST_TOOLS:                  # the clean tool handlers
            _add_bytes(tar, f"tools/{name}/handler.py",
                       (base / "tools" / name / "handler.py").read_bytes())
    return buf.getvalue()


def _add_bytes(tar, arcname: str, data: bytes) -> None:
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mode = 0o644
    tar.addfile(ti, io.BytesIO(data))
