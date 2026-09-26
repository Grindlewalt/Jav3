"""The service box's guest package (DESIGN-BOXES.md (a), WP3).

A service box boots the same baked bootstrap as every guest: it fetches a
package over the gateway and execs `python -m backend.server`. For kind
"service" the gateway answers with THIS package (registered through
gateway_server.register_package_builder("service", build_for_box)), and its
`backend/server.py` is svcd, the service supervisor. It carries no agent loop,
no tool handlers and no model client: a service box has nothing to call them
with (the gateway refuses model_call / tool_broker_call from its CID anyway).

The gateway appends box.json itself (the box's addressing and transport).
procwatch.py (WP4) rides along when it exists so the process view covers
service boxes too.
"""
import io
import tarfile

from ..config import settings


def _svc_src():
    return settings.base_dir / "guest" / "svc"


def build_package_tar() -> bytes:
    base = settings.base_dir
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "backend/__init__.py", b"")
        _add(tar, "backend/server.py", (_svc_src() / "svcd.py").read_bytes())
        pw = base / "guest" / "backend" / "procwatch.py"
        if pw.exists():
            _add(tar, "backend/procwatch.py", pw.read_bytes())
    return buf.getvalue()


def build_for_box(box) -> bytes:
    """gateway_server.register_package_builder signature: fn(box) -> tar.gz.
    The package is the same for every service box; identity is box.json."""
    return build_package_tar()


def _add(tar, arcname: str, data: bytes) -> None:
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mode = 0o644
    tar.addfile(ti, io.BytesIO(data))
