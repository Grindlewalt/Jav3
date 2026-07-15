"""App-owned lifecycle for the disposable guest VM.

The FastAPI app boots QEMU as a subprocess in its own process group (so teardown
kills the whole tree), running a qcow2 overlay on the read-only golden image with
a vhost-vsock channel and NO network device. A guest dies with the app — fine, it
is disposable; nothing durable lives inside. No systemd (that was the old
persistent model); the app owns the guest, which is the path to the per-turn /
pooled guests of Phase 3.
"""
import asyncio
import os
import re
import signal
import socket
from pathlib import Path

from ..config import settings
from .gateway_server import gateway


class VMError(Exception):
    pass


def _base_image() -> Path:
    return settings.vm_dir / f"base-{settings.vm_image_version}.qcow2"


def base_built() -> bool:
    return _base_image().exists()


def _console_log() -> Path:
    return settings.vm_dir / "console.log"


_REPLY_RE = re.compile(r"GUEST-SELFTEST-REPLY: '(.*?)'")
_ERROR_RE = re.compile(r"GUEST-SELFTEST-(?:ERROR|CRASH): (.*)")
_IFACES_RE = re.compile(r"GUEST-NET-IFACES: (\[.*?\])")
_EXTERNAL_RE = re.compile(r"GUEST-NET-EXTERNAL-REACHABLE: (True|False)")


class GuestVM:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None

    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict:
        return {"image_version": settings.vm_image_version,
                "base_built": base_built(),
                "running": self.running(),
                "gateway": gateway.enabled}

    async def _build_overlay(self) -> None:
        base = _base_image()
        if not base.exists():
            raise VMError(f"no golden image {base.name} — run vm/build_base.sh on the Pi")
        overlay = settings.vm_dir / "overlay.qcow2"
        overlay.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "qemu-img", "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2",
            str(overlay), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise VMError(f"overlay create failed: {err.decode(errors='replace')}")

    async def boot(self) -> None:
        if self.running():
            return
        await self._build_overlay()
        _console_log().unlink(missing_ok=True)
        run_vm = settings.base_dir / "vm" / "run_vm.sh"
        env = {**os.environ,
               "VM_DIR": str(settings.vm_dir),
               "JARVIS_VM_BASE": _base_image().name,
               "JARVIS_VM_MEM_MB": str(settings.vm_memory_mb),
               "JARVIS_VM_CPUS": str(settings.vm_cpus),
               "JARVIS_VM_CID": str(settings.vm_guest_cid)}
        self._proc = await asyncio.create_subprocess_exec(
            "bash", str(run_vm), env=env, preexec_fn=os.setsid,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def teardown(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
        self._proc = None
        for name in ("overlay.qcow2", "efi_vars_run.fd", "console.log"):
            (settings.vm_dir / name).unlink(missing_ok=True)

    async def nuke(self) -> None:
        await self.teardown()
        await self.boot()

    async def ensure_ready(self) -> None:
        """Boot the guest if it isn't running and wait until its run-turn server
        accepts a connection. Idempotent — a persistent guest serves many turns
        (a warm pool replaces this in M4)."""
        if not base_built():
            raise VMError("no golden image — run vm/build_base.sh on the Pi first")
        if not gateway.enabled:
            raise VMError("vsock gateway not running (no vsock on this host?)")
        from .guest_turn import GUEST_RUNTURN_PORT
        await self.boot()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + settings.vm_boot_timeout_seconds
        while loop.time() < deadline:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, s.connect, (settings.vm_guest_cid, GUEST_RUNTURN_PORT))
                return
            except OSError:
                await asyncio.sleep(1)
            finally:
                s.close()
        raise VMError("guest run-turn server did not become ready in time")

    def _isolation(self) -> dict:
        text = _console_log().read_text(errors="replace") if _console_log().exists() else ""
        ifaces = _IFACES_RE.search(text)
        external = _EXTERNAL_RE.search(text)
        return {"interfaces": ifaces.group(1) if ifaces else None,
                "external_reachable": (external.group(1) == "True") if external else None}

    async def selftest(self) -> dict:
        """Boot the guest and run ONE real no-tools reasoning turn INSIDE it via
        guest_turn (the loop runs in the guest, its model calls dialing back to
        the host gateway). Returns the guest's answer + the isolation report.
        Tears the guest down after."""
        if not base_built():
            raise VMError("no golden image — run vm/build_base.sh on the Pi first")
        if not gateway.enabled:
            raise VMError("vsock gateway not running (no vsock on this host?)")
        from ..agent.model import confirm_peak
        from .guest_turn import guest_turn
        confirm_peak(0)                        # the turn-level peak decision is the
        # caller's (here, the operator running the selftest); the guest's per-call
        # model_calls then pass the gateway's peak gate, as a host chat turn does.
        await self.boot()
        deadline = asyncio.get_event_loop().time() + settings.vm_boot_timeout_seconds
        final = None
        try:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    async for ev in guest_turn(
                            conversation_id=0,
                            system_prompt="You are terse.",
                            history=[{"role": "user",
                                      "content": "Reply with exactly the word PONG and nothing else."}],
                            op_id="vm-selftest-loop", self_check=False):
                        if ev.get("type") == "final":
                            final = ev.get("content")
                    break
                except (ConnectionError, OSError):
                    await asyncio.sleep(2)      # guest run-turn server not up yet
            isolation = self._isolation()
        finally:
            await self.teardown()
        if final is None:
            raise VMError("guest run-turn server did not become reachable in time")
        return {"reply": final, "isolation": isolation}


# module-level singleton, driven by the vm_api router
vm = GuestVM()
