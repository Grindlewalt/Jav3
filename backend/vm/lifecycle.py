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
import time
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
        # lifecycle transitions (boot/teardown/reap) are serialized so the idle
        # reaper can never nuke a guest a turn is starting on, and two turns never
        # double-boot. `_inflight` counts turns holding the guest; `_idle_since`
        # is when it last fell to zero (the reaper's clock).
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle_since: float | None = None
        self._booted_at: float | None = None

    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict:
        age = int(time.monotonic() - self._booted_at) if self._booted_at else None
        return {"image_version": settings.vm_image_version,
                "base_built": base_built(),
                "running": self.running(),
                "gateway": gateway.enabled,
                "inflight": self._inflight,
                "age_seconds": age,
                "idle_scrub_seconds": settings.vm_idle_scrub_seconds}

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

    async def _kill_orphans(self) -> None:
        """Kill any qemu still holding OUR overlay (hence the guest CID) that we no
        longer track — a guest orphaned across an app restart (setsid detaches it
        from the process group teardown kills). Without this, a reboot's fresh guest
        can't bind the CID and the host would keep talking to the stale one."""
        overlay = str(settings.vm_dir / "overlay.qcow2")
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill", "-9", "-f", overlay,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        except (FileNotFoundError, OSError):
            pass

    async def boot(self) -> None:
        if self.running():
            return
        await self._kill_orphans()
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
        self._booted_at = time.monotonic()
        self._idle_since = time.monotonic()

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
        self._booted_at = None
        await self._kill_orphans()
        for name in ("overlay.qcow2", "efi_vars_run.fd", "console.log"):
            (settings.vm_dir / name).unlink(missing_ok=True)

    async def nuke(self) -> None:
        async with self._lock:
            await self.teardown()
            await self.boot()

    # --- refcount + idle scrub -------------------------------------------------

    async def acquire(self) -> None:
        """Ensure the guest is up and pin it for one turn. Serialized so the reaper
        can't tear down between the readiness check and the pin."""
        async with self._lock:
            await self._ensure_ready_locked()
            self._inflight += 1

    def release(self) -> None:
        """Release one turn's hold; start the idle clock when the last one leaves."""
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0:
            self._idle_since = time.monotonic()

    async def reap_if_idle(self) -> None:
        """If scrubbing is on and the guest has sat idle past the threshold, reboot
        it so the next operation batch starts fresh. No-op while a turn is in
        flight or scrubbing is disabled."""
        window = settings.vm_idle_scrub_seconds
        if not window or not self.running() or self._inflight > 0:
            return
        if self._idle_since is None or time.monotonic() - self._idle_since < window:
            return
        async with self._lock:
            if self._inflight > 0:          # a turn arrived while we waited
                return
            await self.teardown()
            await self.boot()

    async def _ensure_ready_locked(self) -> None:
        """Boot the guest if it isn't running and wait until its run-turn server
        accepts a connection. Caller holds `_lock`. Idempotent — one guest serves
        many turns; the idle reaper reboots it between operation batches."""
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

    async def ensure_ready(self) -> None:
        async with self._lock:
            await self._ensure_ready_locked()

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


async def reaper_loop() -> None:
    """Background: scrub the guest once it has gone idle (M4c). Cheap and inert
    while vm_idle_scrub_seconds is 0. Started from the app lifespan."""
    while True:
        try:
            await asyncio.sleep(settings.vm_reaper_interval_seconds)
            await vm.reap_if_idle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reaper hiccup must never kill the loop
            pass
