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

from ..agent.tools.inguest import BOX_ONLY_TOOLS, IN_GUEST_TOOLS
from ..config import settings

# host modules copied VERBATIM into the guest backend (pure — operate on the
# pushed workspace; no host state). arcname -> repo path.
_COPY_MODULES = {
    "backend/agent/loop.py": "backend/agent/loop.py",
    "backend/agent/imageresult.py": "backend/agent/imageresult.py",
    "backend/codeindex.py": "backend/codeindex.py",
    "backend/agent/tools/todostore.py": "backend/agent/tools/todostore.py",
    "backend/agent/tools/argcheck.py": "backend/agent/tools/argcheck.py",
    # which tools run in the guest: the guest registry routes by it, this file
    # ships the handlers by it, so both read the one list
    "backend/agent/tools/inguest.py": "backend/agent/tools/inguest.py",
    # which file reads count as reading untrusted text (MEM-09): pure, the
    # guest registry asks it after every in-guest tool call
    "backend/agent/tools/taintcheck.py": "backend/agent/tools/taintcheck.py",
    # what the model is shown (tool sections) and the playbooks that ride them:
    # the loop decides both, so a guest turn must decide them the same way
    "backend/agent/tools/toolsections.py": "backend/agent/tools/toolsections.py",
    "backend/navplaybook.py": "backend/navplaybook.py",
    # the memory cgroup + OOM priority run_code and screenshot start their
    # processes under, so a runaway command cannot take the box down with it
    "backend/memguard.py": "backend/memguard.py",
    # the operator's computer-use client, as it is: the box desktop's agent seat
    # (guest/backend/deskbox.py) runs its Session on display :100
    "backend/jav3_desk.py": "clients/jav3-desk/jav3-desk",
}

# IN_GUEST_TOOLS and BOX_ONLY_TOOLS live in backend/agent/tools/inguest.py (imported
# above, so guest_pkg.IN_GUEST_TOOLS still works), shared verbatim with the guest registry.

# guest/backend modules that ship only with boxes on. procwatch (WP4) is only
# ever called by the host's process poller, which is off with the flag; the
# run-turn server's `ps` mode then answers ok:false ("not in the package").
BOX_ONLY_MODULES = frozenset({"procwatch.py"})


def in_guest_tools() -> tuple[str, ...]:
    """IN_GUEST_TOOLS as shipped right now (the box-only ones need the flag)."""
    if settings.vm_boxes_enabled:
        return IN_GUEST_TOOLS
    return tuple(n for n in IN_GUEST_TOOLS if n not in BOX_ONLY_TOOLS)


def _guest_src():
    return settings.base_dir / "guest" / "backend"


def build_package_tar() -> bytes:
    src = _guest_src()
    base = settings.base_dir
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(src.rglob("*.py")):          # checked-in shims + server
            rel = p.relative_to(src).as_posix()
            if rel in BOX_ONLY_MODULES and not settings.vm_boxes_enabled:
                continue
            tar.add(p, arcname=f"backend/{rel}")
        for arcname, relpath in _COPY_MODULES.items():   # verbatim host modules
            _add_bytes(tar, arcname, (base / relpath).read_bytes())
        for name in in_guest_tools():                # the clean tool handlers
            _add_bytes(tar, f"tools/{name}/handler.py",
                       (base / "tools" / name / "handler.py").read_bytes())
    return buf.getvalue()


def _add_bytes(tar, arcname: str, data: bytes) -> None:
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mode = 0o644
    tar.addfile(ti, io.BytesIO(data))
