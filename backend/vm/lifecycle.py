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


_VERSION_RE = re.compile(r"base-v(\d+)\.qcow2$")


def _base_image() -> Path:
    """The active golden image: the HIGHEST base-v<N>.qcow2 present, so a rebuild
    (which creates the next version, never mutating in place) auto-activates on
    the next guest boot. Falls back to the configured version when none exist."""
    versions = [(int(m.group(1)), p) for p in settings.vm_dir.glob("base-v*.qcow2")
                if (m := _VERSION_RE.search(p.name))]
    if versions:
        return max(versions)[1]
    return settings.vm_dir / f"base-{settings.vm_image_version}.qcow2"


def _active_version() -> str:
    m = _VERSION_RE.search(_base_image().name)
    return f"v{m.group(1)}" if m else settings.vm_image_version


def _next_version() -> int:
    versions = [int(m.group(1)) for p in settings.vm_dir.glob("base-v*.qcow2")
                if (m := _VERSION_RE.search(p.name))]
    return (max(versions) if versions else 0) + 1


def _image_meta() -> dict:
    """Wall-clock build time + age of the active image (its file mtime), and
    whether it is past vm_image_max_age_days — the VM widget's amber signal."""
    p = _base_image()
    if not p.exists():
        return {"image_built_at": None, "image_age_days": None, "image_stale": False}
    import datetime
    built = datetime.datetime.fromtimestamp(p.stat().st_mtime)
    age = (datetime.datetime.now() - built).total_seconds() / 86400
    return {"image_built_at": built.isoformat(timespec="seconds"),
            "image_age_days": round(age, 1),
            "image_stale": age > settings.vm_image_max_age_days}


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
        self._rebuilding = False

    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict:
        age = int(time.monotonic() - self._booted_at) if self._booted_at else None
        return {"image_version": _active_version(),
                "base_built": base_built(),
                "running": self.running(),
                "gateway": gateway.enabled,
                "inflight": self._inflight,
                "age_seconds": age,
                "idle_scrub_seconds": settings.vm_idle_scrub_seconds,
                "egress": settings.vm_egress,
                "rebuilding": self._rebuilding,
                **_image_meta()}

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

    async def _net(self, action: str) -> None:
        """Run net_up.sh up|down via passwordless sudo (Pi). Best-effort: a
        failure to bring the net up is logged to console but doesn't wedge boot —
        the guest then simply has no working egress (fails closed)."""
        script = settings.base_dir / "vm" / "net" / "net_up.sh"
        env = {**os.environ,
               "JARVIS_VM_TAP": settings.vm_egress_tap,
               "JARVIS_VM_HOST_IP": settings.vm_egress_host_ip,
               "JARVIS_VM_PCAP": "1" if settings.vm_egress_pcap else "0"}
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "bash", str(script), action, env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode:
                print(f"[egress] net {action} failed: {err.decode(errors='replace')[:300]}")
        except (FileNotFoundError, OSError) as e:
            print(f"[egress] net {action} unavailable: {e}")

    async def _net_up(self) -> None:
        await self._net("up")

    async def _net_down(self) -> None:
        await self._net("down")

    async def boot(self) -> None:
        if self.running():
            return
        await self._kill_orphans()
        await self._build_overlay()
        _console_log().unlink(missing_ok=True)
        if settings.vm_egress:
            await self._net_up()               # tap + nftables + dnsmasq + pcap
        run_vm = settings.base_dir / "vm" / "run_vm.sh"
        env = {**os.environ,
               "VM_DIR": str(settings.vm_dir),
               "JARVIS_VM_BASE": _base_image().name,
               "JARVIS_VM_MEM_MB": str(settings.vm_memory_mb),
               "JARVIS_VM_CPUS": str(settings.vm_cpus),
               "JARVIS_VM_CID": str(settings.vm_guest_cid),
               "JARVIS_VM_EGRESS": "1" if settings.vm_egress else "0"}
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
        if settings.vm_egress:
            await self._net_down()
        for name in ("overlay.qcow2", "efi_vars_run.fd", "console.log"):
            (settings.vm_dir / name).unlink(missing_ok=True)

    async def nuke(self) -> None:
        async with self._lock:
            await self.teardown()
            await self.boot()

    async def rebuild_image(self) -> dict:
        """Build the NEXT golden-image version in the background (vm/build_base.sh,
        ~20 min on the Pi). It never touches the running guest or the current base;
        on success the higher version auto-activates on the next boot. Returns
        immediately — progress streams on the 'vm-rebuild' bus channel."""
        if self._rebuilding:
            return {"started": False, "reason": "a rebuild is already running"}
        version = f"v{_next_version()}"
        self._rebuilding = True
        asyncio.create_task(self._run_rebuild(version))
        return {"started": True, "version": version}

    async def _run_rebuild(self, version: str) -> None:
        from .. import bus
        chan, script = "vm-rebuild", settings.base_dir / "vm" / "build_base.sh"
        env = {**os.environ, "JARVIS_VM_IMAGE_VERSION": version}
        bus.publish(chan, {"type": "rebuild", "phase": "start", "version": version})
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(script), env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            if proc.stdout is not None:
                async for line in proc.stdout:
                    bus.publish(chan, {"type": "rebuild", "phase": "log",
                                       "line": line.decode(errors="replace").rstrip()})
            rc = await proc.wait()
            ok = rc == 0 and (settings.vm_dir / f"base-{version}.qcow2").exists()
            bus.publish(chan, {"type": "rebuild", "phase": "done",
                               "version": version, "ok": ok, "returncode": rc})
        except (FileNotFoundError, OSError) as e:
            bus.publish(chan, {"type": "rebuild", "phase": "error", "error": str(e)})
        finally:
            self._rebuilding = False

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
