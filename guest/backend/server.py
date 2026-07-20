"""The guest run-turn server. Listens on the guest's vsock CID; the host
`guest_turn` connects and sends one `run_turn` spec (newline-delimited JSON);
this runs the real ReAct loop in the guest and streams its four event types
(token / tool / tool_result / final) straight back. Model calls the loop makes
dial back out to the host gateway (see agent/model.py). Nothing durable lives
here — the guest holds no key, no DB, no memory.

Run as: python3 -m backend.server
"""
import asyncio
import base64
import io
import json
import shutil
import socket
import tarfile

from . import config as guest_config
from . import turnctx
from .agent.loop import run_turn

PORT = 5556                                 # guest run-turn server (host dials this)


def _unpack_workspace(slug: str, tar_b64: str) -> None:
    dest = guest_config.settings.projects_dir / slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(tar_b64)), mode="r:gz") as t:
        t.extractall(dest, filter="data")


def _pack_staging(slug: str) -> str:
    staging_dir = guest_config.settings.projects_dir / slug / ".staging"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if staging_dir.is_dir():
            for p in sorted(staging_dir.rglob("*")):
                if p.is_file():
                    tar.add(p, arcname=str(p.relative_to(staging_dir)))
    return base64.b64encode(buf.getvalue()).decode()


async def _handle(loop, conn) -> None:
    tokens = None
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = await loop.sock_recv(conn, 65536)
            if not chunk:
                return
            buf += chunk
        line, _ = buf.split(b"\n", 1)
        spec = json.loads(line)

        async def send(ev: dict) -> None:
            await loop.sock_sendall(conn, (json.dumps(ev) + "\n").encode())

        # prime / pull: an operation that fans out many concurrent leaf turns on one
        # project (an orchestrator team) pushes the workspace ONCE up front and pulls
        # the accumulated .staging ONCE at the end, so the leaves reuse a single copy
        # instead of each racing a fresh unpack.
        mode = spec.get("mode")
        if mode == "prime":
            slug = spec.get("active_slug")
            if slug and spec.get("workspace_tar_b64"):
                _unpack_workspace(slug, spec["workspace_tar_b64"])
            await send({"type": "primed"})
            return
        if mode == "pull":
            slug = spec.get("active_slug")
            await send({"type": "staged", "slug": slug,
                        "tar_b64": _pack_staging(slug) if slug else ""})
            return

        # process-global config knobs (identical every turn); the per-turn state
        # (op_id, tool specs, rules, active slug) is bound task-local below so
        # concurrent turns in this one guest never overwrite each other's.
        guest_config.apply(spec.get("config"))

        # the active project the in-guest file tools operate on. A top-level turn
        # ships the workspace tar and we unpack a fresh copy; a nested turn reuses
        # the copy its parent already pushed (same slug), so it carries only the
        # slug and we neither unpack (which would wipe the parent's staged edits)
        # nor pack — the top-level turn packs the shared .staging at its end.
        slug = spec.get("active_slug")
        owns_workspace = bool(slug and spec.get("workspace_tar_b64"))
        if owns_workspace:
            _unpack_workspace(slug, spec["workspace_tar_b64"])
        tokens = turnctx.enter(spec, slug)

        try:
            async for ev in run_turn(
                    spec.get("conversation_id") or 0,
                    spec["system_prompt"],
                    spec.get("history") or [],
                    tools=spec.get("tool_specs"),
                    model_name=spec.get("model_name"),
                    base_url=spec.get("base_url"),
                    self_check=spec.get("self_check", True),
                    max_iterations=spec.get("max_iterations"),
                    on_tool_call=None):
                await send(ev)
        except Exception as e:  # noqa: BLE001 — surface any loop crash as a final
            await send({"type": "final",
                        "content": f"(guest loop error: {type(e).__name__}: {e})"})

        # ship the guest's staged edits back for host-side reconcile + approval
        if owns_workspace:
            await send({"type": "staged", "slug": slug, "tar_b64": _pack_staging(slug)})
    except (ConnectionError, OSError):
        pass
    finally:
        if tokens is not None:
            turnctx.reset(tokens)
        try:
            conn.close()
        except OSError:
            pass


def _detect_egress_proxy() -> None:
    """Monitored-egress mode: the guest has a tap NIC (10.201.0.2, gateway
    10.201.0.1 — mirrors host vm_egress_host_ip/proxy_port). Point every
    subprocess (pip/npm/curl/git via run_code) at the host proxy. A netless guest
    has only lo, so this stays unset and direct sockets fail closed as before."""
    import os
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.201.0.1", 9))     # no packet sent; resolves src addr
            local = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        local = ""
    if local.startswith("10.201."):
        proxy = "http://10.201.0.1:8443"
        os.environ["JARVIS_EGRESS_PROXY"] = proxy
        os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                          http_proxy=proxy, https_proxy=proxy)
        print(f"GUEST-EGRESS-PROXY: {proxy}", flush=True)
    else:
        print("GUEST-EGRESS-PROXY: none (netless)", flush=True)


async def serve() -> None:
    _detect_egress_proxy()
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    s.bind((socket.VMADDR_CID_ANY, PORT))
    s.listen(4)
    s.setblocking(False)
    print(f"GUEST-RUNTURN-SERVER: listening on vsock :{PORT}", flush=True)
    while True:
        conn, _ = await loop.sock_accept(s)
        conn.setblocking(False)
        asyncio.create_task(_handle(loop, conn))


if __name__ == "__main__":
    asyncio.run(serve())
