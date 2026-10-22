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

from . import boxinfo
from . import config as guest_config
from . import persist, turnctx
from .agent.loop import run_turn
from .fsutil import safe_join

PORT = 5556                                 # guest run-turn server (host dials this)


# slug -> the files the host put in the workspace copy, plus every file staged
# since. Deletions are measured against it: the shell's rm and mv act on the copy
# (write_file buffers into .staging), so a file that is in here but no longer in
# the tree or the buffer was deleted or renamed away, and the host is told.
# In memory on purpose: nothing the agent runs can reach this process.
_known: dict[str, set[str]] = {}

# the member of the staged tarball that carries that list; `.staging` is a name
# the guest's own write tools refuse, so no file the agent writes can collide
DELETED_MEMBER = ".staging/deleted.json"


def _unpack_workspace(slug: str, tar_b64: str) -> None:
    dest = guest_config.settings.projects_dir / slug
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(tar_b64)), mode="r:gz") as t:
        t.extractall(dest, filter="data")
        _known[slug] = {m.name for m in t.getmembers() if m.isfile()}


def _put_files(slug, files) -> dict:
    """Write host-sent files (rel -> base64) into the workspace copy, beside what
    is there. The turn's own pending copy of a path is dropped: the host's text
    was written after it and is canonical. Never creates the workspace, and
    never writes into .staging or .git."""
    root = guest_config.settings.projects_dir / slug if isinstance(slug, str) and slug else None
    if root is None or not root.is_dir() or not isinstance(files, dict):
        return {"type": "put", "ok": False, "error": "no workspace for that project here"}
    done, refused = [], {}
    for rel, b64 in files.items():
        try:
            if not isinstance(rel, str) or not isinstance(b64, str):
                raise ValueError("not a path and bytes")
            data = base64.b64decode(b64)
            dest = safe_join(root, rel)
            if dest.relative_to(root.resolve()).parts[0] in (".staging", ".git"):
                raise ValueError("a protected path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(0o644)
            pending = safe_join(root / ".staging", rel)
            if pending.is_file():
                pending.unlink()
            _known.setdefault(slug, set()).add(rel)
            done.append(rel)
        except Exception as e:  # noqa: BLE001 — one bad path must not drop the rest
            refused[str(rel)[:120]] = f"{type(e).__name__}: {e}"[:120]
    return {"type": "put", "ok": not refused, "written": done, "refused": refused}


def _pack_staging(slug: str) -> str:
    root = guest_config.settings.projects_dir / slug
    staging_dir = root / ".staging"
    buf = io.BytesIO()
    staged: set[str] = set()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if staging_dir.is_dir():
            for p in sorted(staging_dir.rglob("*")):
                if p.is_file():
                    rel = str(p.relative_to(staging_dir))
                    staged.add(rel)
                    tar.add(p, arcname=rel)
        known = _known.setdefault(slug, set())
        known |= staged
        # a staged file is a write, not a deletion, even if the tree copy is gone
        gone = sorted(r for r in known if r not in staged and not (root / r).is_file())
        if gone:
            data = json.dumps(gone).encode()
            ti = tarfile.TarInfo(DELETED_MEMBER)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
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
        # approved persistence: the host has just hot-plugged (or is about to
        # unplug) the project's /persist disk; mount/unmount it here. Blocking
        # tools (mkfs, mount) run in a thread so other turns keep streaming.
        if mode == "persist_mount":
            await send(await asyncio.to_thread(
                persist.mount, bool(spec.get("persist_ro")),
                bool(spec.get("persist_fresh"))))
            return
        if mode == "persist_unmount":
            await send(await asyncio.to_thread(persist.unmount))
            return
        # process telemetry (WP4): the host polls every box for its process
        # tree. procwatch.py ships in the package once WP4 lands; until then
        # (or on a guest without it) the reply says so rather than failing.
        if mode == "ps":
            try:
                from . import procwatch
                snap = await asyncio.to_thread(procwatch.snapshot)
                await send({"type": "ps", "ok": True, "snapshot": snap})
            except Exception as e:  # noqa: BLE001 — a poll must never kill the server
                await send({"type": "ps", "ok": False,
                            "error": f"{type(e).__name__}: {e}"[:300]})
            return
        # kill one process the host's last ps snapshot named: only if /proc still
        # shows the same program and start time (procwatch.kill_pid); a pid that
        # was reused is refused, never killed
        if mode == "kill_pid":
            try:
                from . import procwatch
                out = await asyncio.to_thread(
                    procwatch.kill_pid, spec.get("pid"), spec.get("exe"),
                    spec.get("start_ticks"), spec.get("cmd"), spec.get("sig"))
                await send({"type": "kill", **out})
            except Exception as e:  # noqa: BLE001 — answer, never kill the server
                await send({"type": "kill", "ok": False, "why": "error",
                            "error": f"{type(e).__name__}: {e}"[:300]})
            return
        # put_files: the host wrote a file mid-turn (research's document) and told
        # the model to read it; the copy was built at turn start, so it lands here.
        if mode == "put_files":
            await send(_put_files(spec.get("active_slug"), spec.get("files")))
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
                    # the host sets `persist` only when it mounted the disk
                    spec["system_prompt"] + persist.note(spec.get("persist")),
                    spec.get("history") or [],
                    tools=spec.get("tool_specs"),
                    model_name=spec.get("model_name"),
                    base_url=spec.get("base_url"),
                    self_check=spec.get("self_check", True),
                    rewrite_rules=spec.get("rewrite_rules", True),
                    inject_rules=spec.get("inject_rules", True),
                    max_iterations=spec.get("max_iterations"),
                    # WP5: drain messages other agents addressed to this turn
                    # between iterations. The drain brokers to the host, which
                    # resolves WHOSE inbox from the op_id envelope — the guest
                    # supplies no identity and so cannot read another's.
                    inbox=spec.get("inbox", False),
                    on_tool_call=None):
                await send(ev)
        except Exception as e:  # noqa: BLE001 — surface any loop crash as a final
            # `error` marks it as a failure, not an answer: the host raises it
            # (after the edits come home) so the chat shows an error; `content`
            # keeps older hosts working
            await send({"type": "final",
                        "content": f"(guest loop error: {type(e).__name__}: {e})",
                        "error": f"{type(e).__name__}: {e}"})

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


def _bring_up_egress_nic() -> None:
    """Give the tap NIC its pinned egress address. The golden image is built
    netless (cloud-init masks networkd-wait-online; the runtime boot runs no DHCP
    client), so enp0s1 comes up with no address and the guest can't reach the host
    proxy/DNS at 10.201.0.1. Assign the static 10.201.0.2/24 the host already pins
    (dnsmasq dhcp-host in vm/net/dnsmasq-egress.conf) so the proxy path works. A
    netless guest has only lo -> no-op. Idempotent, best-effort, runs as root."""
    import os
    import subprocess
    # box.json (multi-box mode) names this box's /30; absent, the shared
    # guest's fixed 10.201.0.2/24 via 10.201.0.1 exactly as before
    n = boxinfo.net()
    guest_ip, host_ip, prefix = n["guest_ip"], n["gateway"], n["prefix"]
    nics = [n for n in sorted(os.listdir("/sys/class/net")) if n != "lo"]
    if not nics:
        return
    nic = nics[0]
    try:
        have = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", nic],
                              capture_output=True, text=True).stdout
        if f" {guest_ip}/" not in have:
            subprocess.run(["ip", "addr", "flush", "dev", nic], check=False)
            subprocess.run(["ip", "addr", "add", f"{guest_ip}/{prefix}", "dev", nic],
                           check=False)
        subprocess.run(["ip", "link", "set", nic, "up"], check=False)
        subprocess.run(["ip", "route", "replace", "default", "via", host_ip,
                        "dev", nic], check=False)
        try:
            with open("/etc/resolv.conf", "w") as f:
                f.write(f"nameserver {n['dns']}\n")
        except OSError:
            pass
        print(f"GUEST-EGRESS-NIC: {nic} {guest_ip}/{prefix} via {host_ip}", flush=True)
    except Exception as e:  # noqa: BLE001 — never let NIC setup crash the boot
        print(f"GUEST-EGRESS-NIC-ERROR: {type(e).__name__}: {e}", flush=True)


def _detect_egress_proxy() -> None:
    """Monitored-egress mode: the guest has a tap NIC (10.201.0.2, gateway
    10.201.0.1 — mirrors host vm_egress_host_ip/proxy_port). Point every
    subprocess (pip/npm/curl/git via run_code) at the host proxy. A netless guest
    has only lo, so this stays unset and direct sockets fail closed as before."""
    import os
    n = boxinfo.net()
    if boxinfo.load().get("runtime") == "docker":
        # --network none: no route to probe. The proxy is the in-container
        # forwarder on loopback (box.json net.proxy), spliced to proxy.sock.
        proxy = n["proxy"]
        local = "localhost,127.0.0.1,::1"
        os.environ["JARVIS_EGRESS_PROXY"] = proxy
        os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                          http_proxy=proxy, https_proxy=proxy,
                          NO_PROXY=local, no_proxy=local)
        print(f"GUEST-EGRESS-PROXY: {proxy} (docker forwarder)", flush=True)
        return
    _bring_up_egress_nic()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((n["gateway"], 9))     # no packet sent; resolves src addr
            local = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        local = ""
    if local.startswith("10.201."):
        proxy = n["proxy"]
        os.environ["JARVIS_EGRESS_PROXY"] = proxy
        local = "localhost,127.0.0.1,::1"    # loopback stays in-guest, never proxied
        os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                          http_proxy=proxy, https_proxy=proxy,
                          NO_PROXY=local, no_proxy=local)
        print(f"GUEST-EGRESS-PROXY: {proxy}", flush=True)
    else:
        print("GUEST-EGRESS-PROXY: none (netless)", flush=True)


async def serve() -> None:
    _detect_egress_proxy()
    loop = asyncio.get_running_loop()
    # vsock ANY:5556, or a docker box's /run/jav3/5556.sock (box.json)
    s = boxinfo.listen("runturn", PORT)
    b = boxinfo.load()
    if b:
        print(f"GUEST-BOX: {b.get('id')} kind={b.get('kind')} "
              f"runtime={b.get('runtime')}", flush=True)
    print(f"GUEST-RUNTURN-SERVER: listening on vsock :{PORT}", flush=True)
    # the operator co-working PTY listener runs alongside (best-effort: an old
    # golden image without shell.py just skips it, run-turn still serves)
    try:
        from . import shell
        asyncio.ensure_future(shell.serve())
    except Exception as e:  # noqa: BLE001
        print(f"GUEST-SHELL-SERVER: not started ({e})", flush=True)
    # the live desktop listener (display.py): same best-effort rule, and it only
    # starts Xvnc when a viewer asks, so a box nobody watches pays nothing
    try:
        from . import display
        asyncio.ensure_future(display.serve())
    except Exception as e:  # noqa: BLE001
        print(f"GUEST-DISPLAY-SERVER: not started ({e})", flush=True)
    while True:
        conn, _ = await loop.sock_accept(s)
        conn.setblocking(False)
        asyncio.create_task(_handle(loop, conn))


if __name__ == "__main__":
    asyncio.run(serve())
