"""Host<->VM bridge for the sandbox VM.

The VM is pure scratch space: QEMU overlay on a read-only golden image,
reachable only via SSH forwarded to loopback, host key pinned at image-build
time. Nothing durable lives inside — files go in with push(), results come
back with pull(), and nuke() throws the whole disk away and boots fresh.

Lifecycle is owned by the systemd user unit (jarvis-vm.service); this module
drives it with `systemctl --user` and talks to the guest with asyncssh.
"""
from __future__ import annotations

import asyncio
import io
import shlex
import tarfile
from pathlib import Path

import asyncssh

from ...config import settings

# Never worth shipping into the VM (and .git would leak history the VM has
# no business seeing — the VM never touches git, per spec).
PUSH_EXCLUDE = {".git", ".venv", "node_modules", "__pycache__", "dist",
                ".workspace.json", ".staging"}


class VMError(Exception):
    pass


def base_built() -> bool:
    return (settings.vm_dir / "base.qcow2").exists()


def overlay_exists() -> bool:
    return (settings.vm_dir / "overlay.qcow2").exists()


async def _systemctl(*args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 1, "systemctl unavailable"
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def unit_active() -> bool:
    code, _ = await _systemctl("is-active", "--quiet", settings.vm_unit)
    return code == 0


async def _connect(timeout: float = 10) -> asyncssh.SSHClientConnection:
    key = settings.vm_dir / "agent_ed25519"
    known = settings.vm_dir / "known_hosts"
    if not key.exists() or not known.exists():
        raise VMError("VM keys missing — run vm/build_base.sh on the host first")
    try:
        return await asyncio.wait_for(
            asyncssh.connect(
                settings.vm_ssh_host, port=settings.vm_ssh_port,
                username=settings.vm_ssh_user,
                client_keys=[str(key)], known_hosts=str(known)),
            timeout=timeout)
    except asyncssh.HostKeyNotVerifiable as e:
        # A changed host key on a VM we baked ourselves means the disk is not
        # the one we built. Don't retry around this — surface it loudly.
        raise VMError(f"VM host key mismatch — possible compromise, nuke advised: {e}")
    except (OSError, asyncio.TimeoutError, asyncssh.Error) as e:
        raise VMError(f"VM unreachable over SSH: {e}")


async def ssh_ready() -> bool:
    try:
        conn = await _connect(timeout=5)
        conn.close()
        return True
    except VMError:
        return False


async def status() -> dict:
    active = await unit_active()
    return {
        "base_built": base_built(),
        "overlay_exists": overlay_exists(),
        "unit_active": active,
        "ssh_ready": (await ssh_ready()) if active else False,
    }


async def wait_ready(timeout: float | None = None) -> None:
    deadline = asyncio.get_event_loop().time() + (timeout or settings.vm_boot_timeout_seconds)
    while asyncio.get_event_loop().time() < deadline:
        if await ssh_ready():
            return
        await asyncio.sleep(2)
    raise VMError("VM did not become SSH-reachable in time")


async def start(wait: bool = True) -> None:
    if not base_built():
        raise VMError("no golden image — run vm/build_base.sh on the host first")
    code, out = await _systemctl("start", settings.vm_unit)
    if code != 0:
        raise VMError(f"systemctl start failed: {out}")
    if wait:
        await wait_ready()


async def stop() -> None:
    code, out = await _systemctl("stop", settings.vm_unit)
    if code != 0:
        raise VMError(f"systemctl stop failed: {out}")


async def nuke(wait: bool = True) -> None:
    """Recovery action: discard the VM's entire disk state and boot fresh
    from the golden image. Not part of the normal run cycle."""
    await stop()
    for name in ("overlay.qcow2", "efi_vars_run.fd", "console.log"):
        (settings.vm_dir / name).unlink(missing_ok=True)
    await start(wait=wait)


async def run(command: str, timeout: float | None = None,
              cwd: str | None = None, input: str | None = None) -> dict:
    """Run a shell command in the VM as the agent user. Returns exit status
    and captured output; a timeout kills the command, not the VM."""
    timeout = timeout or settings.vm_run_timeout_seconds
    workdir = cwd or settings.vm_workspace
    conn = await _connect()
    try:
        full = f"cd {shlex.quote(workdir)} && {command}"
        try:
            result = await asyncio.wait_for(
                conn.run(full, check=False, input=input), timeout=timeout)
        except asyncio.TimeoutError:
            return {"exit_status": -1, "stdout": "", "stderr": "",
                    "timed_out": True, "timeout": timeout}
        return {"exit_status": result.exit_status,
                "stdout": result.stdout or "", "stderr": result.stderr or "",
                "timed_out": False}
    finally:
        conn.close()


def _make_tar(local_dir: Path) -> bytes:
    buf = io.BytesIO()
    max_bytes = settings.vm_push_max_mb * 1024 * 1024
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(local_dir.rglob("*")):
            rel = path.relative_to(local_dir)
            if any(part in PUSH_EXCLUDE for part in rel.parts):
                continue
            tar.add(path, arcname=str(rel), recursive=False)
            if buf.tell() > max_bytes:
                raise VMError(f"push exceeds {settings.vm_push_max_mb}MB limit")
    return buf.getvalue()


async def push(local_dir: Path, remote_dir: str) -> dict:
    """Copy a host directory into the VM (tar over SSH). remote_dir is
    created fresh under the VM workspace — pushes replace, never merge."""
    if not local_dir.is_dir():
        raise VMError(f"not a directory: {local_dir}")
    dest = f"{settings.vm_workspace}/{remote_dir}".rstrip("/")
    data = _make_tar(local_dir)
    conn = await _connect()
    try:
        cmd = (f"rm -rf {shlex.quote(dest)} && mkdir -p {shlex.quote(dest)} "
               f"&& tar -xzf - -C {shlex.quote(dest)}")
        result = await conn.run(cmd, input=data, check=False, encoding=None)
        if result.exit_status != 0:
            stderr = (result.stderr or b"").decode(errors="replace")
            raise VMError(f"push failed: {stderr}")
        return {"pushed_to": dest, "bytes": len(data)}
    finally:
        conn.close()


async def pull(remote_path: str, local_dir: Path) -> dict:
    """Copy a file or directory out of the VM workspace into a host
    directory. Tar members are extracted with the 'data' filter, so the VM
    cannot plant absolute paths, .. escapes, or device nodes on the host."""
    src = f"{settings.vm_workspace}/{remote_path}".rstrip("/")
    conn = await _connect()
    try:
        cmd = (f"cd $(dirname {shlex.quote(src)}) "
               f"&& tar -czf - $(basename {shlex.quote(src)})")
        result = await conn.run(cmd, check=False, encoding=None)
        if result.exit_status != 0:
            stderr = (result.stderr or b"").decode(errors="replace")
            raise VMError(f"pull failed: {stderr}")
        data = result.stdout or b""
        if len(data) > settings.vm_push_max_mb * 1024 * 1024:
            raise VMError(f"pull exceeds {settings.vm_push_max_mb}MB limit")
    finally:
        conn.close()
    local_dir.mkdir(parents=True, exist_ok=True)
    names = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(local_dir, filter="data")
        names = tar.getnames()
    return {"pulled_to": str(local_dir), "bytes": len(data),
            "files": len([n for n in names if n])}
