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
import subprocess
import time
from pathlib import Path

from ..config import settings
from . import boxes as _boxes_mod
from .gateway_server import gateway
from .persist import status as persist_status


class VMError(Exception):
    pass


_VERSION_RE = re.compile(r"base-v(\d+)\.qcow2$")
REBUILD_LOG_LINES = 400           # the base rebuild's log kept for the Images tab


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


def blockers() -> list[str]:
    """Everything that stops an agent turn on this host, all at once, so the
    operator does not fix KVM only to discover the missing key next."""
    out = []
    if not os.path.exists("/dev/kvm"):
        out.append("no /dev/kvm (CPU virtualization off in BIOS, or the kvm module "
                   f"not loaded; `bash {settings.base_dir}/scripts/install.sh --check` says which)")
    if not os.path.exists("/dev/vhost-vsock"):
        out.append("no /dev/vhost-vsock (sudo modprobe vhost_vsock)")
    if not base_built():
        out.append("no guest image (VM_DIR=%s bash %s/vm/build_base.sh, once KVM works)"
                   % (settings.vm_dir, settings.base_dir))
    try:
        from .. import providers
        pid = providers.default_provider()
        if providers.needs_key(providers.provider(pid)) and not providers.api_key(pid):
            out.append(f"no API key for the default provider {pid} (Settings → Providers)")
    except Exception:   # noqa: BLE001 — the list is advice; never let it raise
        pass
    return out


def no_image_message() -> str:
    """Why there is no guest image, and the next step, from facts on this host:
    without /dev/kvm build_base.sh cannot run either, so saying "run it" alone
    sends the operator into a second failure."""
    b = blockers()
    if len(b) > 1:
        return "cannot run an agent turn on this host yet:\n" + "\n".join(
            f"  {i}. {x}" for i, x in enumerate(b, 1))
    build = (f"VM_DIR={settings.vm_dir} bash {settings.base_dir}/vm/build_base.sh")
    if not os.path.exists("/dev/kvm"):
        return ("no golden image, and it cannot be built yet: /dev/kvm is missing "
                "(CPU virtualization off in BIOS, or the kvm module not loaded; "
                f"`bash {settings.base_dir}/scripts/install.sh --check` says which). "
                f"Once it exists: {build}")
    return f"no golden image — build it (about 10 min): {build}"


def _console_log() -> Path:
    return settings.vm_dir / "console.log"


_REPLY_RE = re.compile(r"GUEST-SELFTEST-REPLY: '(.*?)'")
_ERROR_RE = re.compile(r"GUEST-SELFTEST-(?:ERROR|CRASH): (.*)")
_IFACES_RE = re.compile(r"GUEST-NET-IFACES: (\[.*?\])")
_EXTERNAL_RE = re.compile(r"GUEST-NET-EXTERNAL-REACHABLE: (True|False)")


class GuestVM:
    """One KVM guest. `GuestVM()` is the shared box (module singleton `vm`,
    today's files and constants); `GuestVM(box)` runs a non-shared box from
    backend/vm/boxes.py in its own directory, CID, tap and MAC."""

    def __init__(self, box=None):
        self.box = box if box is not None and not box.is_shared else None
        self._proc: asyncio.subprocess.Process | None = None
        # lifecycle transitions (boot/teardown/reap) are serialized so the idle
        # reaper can never nuke a guest a turn is starting on, and two turns never
        # double-boot. `_inflight` counts turns holding the guest; `_idle_since`
        # is when it last fell to zero (the reaper's clock).
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle_since: float | None = None
        self._booted_at: float | None = None
        # booted and not used since: a scrub would only reboot a fresh guest
        # (every window, forever, and a "wiped" history row each time)
        self._fresh = False
        self._rebuilding = False
        # the /vms rows: booting but not yet serving, and the last boot error
        # (cleared by the next good start)
        self.starting = False
        self.error: str | None = None
        # the base image's last rebuild (build_base.sh): its log survives in
        # memory for the Images tab; {version, running, ok, returncode, lines}
        self.rebuild_log: dict = {}

    def _record_box(self):
        """The Box this controller runs, for the history (the shared box has
        none of its own: boxes.shared())."""
        return self.box if self.box is not None else _boxes_mod.shared()

    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    # --- per-box identity (None box = the shared guest, exactly as before) ----
    @property
    def _dir(self) -> Path:
        return self.box.dir if self.box is not None else settings.vm_dir

    @property
    def _cid(self) -> int:
        return self.box.cid if self.box is not None else settings.vm_guest_cid

    def _console(self) -> Path:
        return self._dir / "console.log"

    # the controller interface boxes.py reads (docs/boxes-contract.md, B)
    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def idle_since(self) -> float | None:
        return self._idle_since

    @property
    def booted_at(self) -> float | None:
        return self._booted_at

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running() else None

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
                "blockers": blockers(),
                "persist": persist_status(),
                **_image_meta()}

    def _image(self) -> Path:
        if self.box is None:
            return _base_image()
        from . import boxes
        try:
            return boxes.image_path(self.box)
        except boxes.BoxError as e:
            raise VMError(str(e))

    async def _build_overlay(self) -> None:
        base = self._image()
        if not base.exists():
            raise VMError(no_image_message() if self.box is None
                          else f"no image {base.name} for box {self.box}")
        self._dir.mkdir(parents=True, exist_ok=True)
        overlay = self._dir / "overlay.qcow2"
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
        overlay = str(self._dir / "overlay.qcow2")
        # a blocking run in a thread, bounded: under the stdlib asyncio loop at
        # shutdown the child watcher may never deliver pkill's exit, and
        # `await proc.wait()` hung forever on a <defunct> child (e2e BUG-4)
        try:
            await asyncio.wait_for(asyncio.to_thread(
                subprocess.run, ["pkill", "-9", "-f", overlay],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10), 15)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired,
                asyncio.TimeoutError):
            pass

    async def _net(self, action: str) -> None:
        """Run net_up.sh up|down via passwordless sudo (Pi). Best-effort: a
        failure to bring the net up is logged to console but doesn't wedge boot —
        the guest then simply has no working egress (fails closed)."""
        # argv, not env: sudo's env_reset strips JARVIS_*, and the script's
        # defaults are the default install's live tap/table (e2e BUG-1)
        from . import boxnet
        try:
            argv = boxnet.net_up_argv(action)
        except ValueError as e:
            print(f"[egress] net {action} refused: {e}")
            return
        try:
            # thread + timeout, like _kill_orphans: net down runs at shutdown,
            # where an awaited child exit may never be delivered (e2e BUG-4)
            r = await asyncio.to_thread(
                subprocess.run, argv, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=120)
            if r.returncode:
                print(f"[egress] net {action} failed: "
                      f"{r.stderr.decode(errors='replace')[:300]}")
        except subprocess.TimeoutExpired:
            print(f"[egress] net {action} timed out")
        except (FileNotFoundError, OSError) as e:
            print(f"[egress] net {action} unavailable: {e}")

    async def net_up(self) -> None:
        """Bring the monitored-egress network up. Called ONCE from the app
        lifespan when vm_egress is on — before the proxy binds its host IP and
        before any guest boots."""
        # multi-box mode loads the set-pinned ruleset + a resolver on jvtap*
        await self._net("up-boxes" if settings.vm_boxes_enabled else "up")

    async def net_down(self) -> None:
        await self._net("down-boxes" if settings.vm_boxes_enabled else "down")

    async def boot(self) -> None:
        if self.running():
            return
        await self._kill_orphans()
        await self._build_overlay()
        if self.box is not None:
            self.box.booted_image = (self.box.image[0], _boxes_mod.image_version(
                self.box.image[0], self.box.image[1]))
        self._console().unlink(missing_ok=True)
        if self.box is not None:
            await self._box_net_up()
        # the monitored-egress network (tap/nft/dnsmasq/proxy) is APP-lifecycle,
        # not per-boot — it's up before any guest and survives idle-scrub reboots,
        # so the proxy's host-IP binding never flaps mid-operation. run_vm.sh
        # attaches to the pre-existing jvtap0.
        run_vm = settings.base_dir / "vm" / "run_vm.sh"
        env = {**os.environ,
               "VM_DIR": str(settings.vm_dir),
               "JARVIS_VM_BASE": _base_image().name,
               "JARVIS_VM_MEM_MB": str(settings.vm_memory_mb),
               "JARVIS_VM_CPUS": str(settings.vm_cpus),
               "JARVIS_VM_CID": str(settings.vm_guest_cid),
               "JARVIS_VM_EGRESS": "1" if settings.vm_egress else "0"}
        if settings.vm_egress and self.box is None:
            # the shared box attaches to ITS tap (vm_egress_tap), never a
            # hard-coded jvtap0 that may belong to another instance
            from . import boxnet
            try:
                env["JARVIS_VM_TAP"] = boxnet.shared_net()["tap"]
            except ValueError as e:
                raise VMError(f"egress network misconfigured: {e}")
        if self.box is not None:
            # a non-shared box: its own dir, image path, CID, tap and MAC
            env.update({"VM_DIR": str(self._dir),
                        "JARVIS_VM_BASE": str(self._image()),
                        "JARVIS_VM_MEM_MB": str(self.box.mem_mb),
                        "JARVIS_VM_CPUS": str(self.box.cpus),
                        "JARVIS_VM_CID": str(self.box.cid),
                        "JARVIS_VM_TAP": self.box.tap,
                        "JARVIS_VM_MAC": self.box.mac})
        self._proc = await asyncio.create_subprocess_exec(
            "bash", str(run_vm), env=env, preexec_fn=os.setsid,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self._booted_at = time.monotonic()
        self._idle_since = time.monotonic()
        self._fresh = True
        if self.box is None:
            # a non-shared box's boot is recorded at box_up (boxes._emit)
            from . import boxlog
            await boxlog.happened(self._record_box(), "started")

    async def teardown(self) -> None:
        was_running = self.running()
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
        if self.box is None:
            # QEMU is gone, so any /persist disk it had attached is closed
            from . import persist
            persist.forget()
        for name in ("overlay.qcow2", "efi_vars_run.fd", "console.log", "qmp.sock"):
            (self._dir / name).unlink(missing_ok=True)
        if self.box is not None:
            await self._box_net_down()
        elif was_running:
            from . import boxlog
            await boxlog.happened(self._record_box(), "stopped")

    async def _box_net_up(self) -> None:
        """A non-shared box's network (tap + nft pins) and the box_up hooks
        (WP2's proxy listener), BEFORE the guest boots. Any failure here
        unwinds and refuses the boot: a box with a half-built boundary is
        never started."""
        from . import boxes, boxnet
        try:
            if settings.vm_egress:
                await boxnet.add(self.box)
            await boxes.box_up(self.box)
        except Exception as e:
            await self._box_net_down()
            raise VMError(f"box {self.box.id} network failed: {e}")

    async def _box_net_down(self) -> None:
        from . import boxes, boxnet
        if settings.vm_egress:
            await boxnet.delete(self.box)
        await boxes.box_down(self.box)

    async def nuke(self) -> None:
        from . import boxlog
        async with boxlog.action(self._record_box(), "nuked",
                                 reason="overlay discarded, rebooted from the golden image"):
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
        env = {**os.environ, "JARVIS_VM_IMAGE_VERSION": version,
               "VM_DIR": str(settings.vm_dir)}
        bus.publish(chan, {"type": "rebuild", "phase": "start", "version": version})
        lines: list[str] = []
        self.rebuild_log = {"version": version, "running": True, "ok": None,
                            "returncode": None, "error": None, "lines": lines,
                            "started_at": time.time(), "finished_at": None}
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(script), env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            if proc.stdout is not None:
                async for line in proc.stdout:
                    text = line.decode(errors="replace").rstrip()
                    lines.append(text[:400])
                    del lines[:-REBUILD_LOG_LINES]
                    bus.publish(chan, {"type": "rebuild", "phase": "log", "line": text})
            rc = await proc.wait()
            ok = rc == 0 and (settings.vm_dir / f"base-{version}.qcow2").exists()
            self.rebuild_log.update(ok=ok, returncode=rc)
            bus.publish(chan, {"type": "rebuild", "phase": "done",
                               "version": version, "ok": ok, "returncode": rc})
        except (FileNotFoundError, OSError) as e:
            self.rebuild_log.update(ok=False, error=str(e))
            bus.publish(chan, {"type": "rebuild", "phase": "error", "error": str(e)})
        finally:
            self.rebuild_log.update(running=False, finished_at=time.time())
            self._rebuilding = False

    # --- refcount + idle scrub -------------------------------------------------

    async def acquire(self) -> None:
        """Ensure the guest is up and pin it for one turn. Serialized so the reaper
        can't tear down between the readiness check and the pin."""
        async with self._lock:
            self.starting = not self.running()
            try:
                await self._ensure_ready_locked()
            except VMError as e:
                self.error = str(e)
                from . import boxlog
                await boxlog.happened(self._record_box(), "error", reason=str(e))
                raise
            finally:
                self.starting = False
            self.error = None
            self._fresh = False
            self._inflight += 1

    def release(self) -> None:
        """Release one turn's hold; start the idle clock when the last one leaves."""
        self._inflight = max(0, self._inflight - 1)
        if self._inflight == 0:
            self._idle_since = time.monotonic()

    async def reap_if_idle(self) -> None:
        """If scrubbing is on and the guest has sat idle past the threshold, reboot
        it so the next operation batch starts fresh. No-op while a turn is in
        flight, while scrubbing is disabled, and once it is fresh: a guest nothing
        has used since its last boot has nothing to scrub."""
        window = settings.vm_idle_scrub_seconds
        if not window or not self.running() or self._inflight > 0 or self._fresh:
            return
        if self._idle_since is None or time.monotonic() - self._idle_since < window:
            return
        from . import boxlog
        async with self._lock:
            if self._inflight > 0:          # a turn arrived while we waited
                return
            async with boxlog.action(self._record_box(), "wiped", actor="reaper",
                                     reason=f"idle scrub after {_boxes_mod._mins(window)}"):
                await self.teardown()
                await self.boot()

    async def _ensure_ready_locked(self) -> None:
        """Boot the guest if it isn't running and wait until its run-turn server
        accepts a connection. Caller holds `_lock`. Idempotent — one guest serves
        many turns; the idle reaper reboots it between operation batches."""
        if not base_built():
            raise VMError(no_image_message())
        if not gateway.enabled:
            raise VMError("vsock gateway not running (no vsock on this host?)")
        from .guest_turn import GUEST_RUNTURN_PORT
        ready_port = GUEST_RUNTURN_PORT
        if self.box is not None and self.box.kind == "service":
            # a service box runs svcd (5558), not the run-turn server
            from .boxes import PORT_SVCD
            ready_port = PORT_SVCD
        await self.boot()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + settings.vm_boot_timeout_seconds
        while loop.time() < deadline:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, s.connect, (self._cid, ready_port))
                return
            except OSError:
                await asyncio.sleep(1)
            finally:
                s.close()
        raise VMError("guest run-turn server did not become ready in time")

    def _isolation(self) -> dict:
        log = self._console()
        text = log.read_text(errors="replace") if log.exists() else ""
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
            raise VMError(no_image_message())
        if not gateway.enabled:
            raise VMError("vsock gateway not running (no vsock on this host?)")
        from .guest_turn import guest_turn
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


# module-level singleton, driven by the vm_api router: the SHARED box
vm = GuestVM()
_boxes_mod.register_runtime("kvm", GuestVM)


async def reaper_loop() -> None:
    """Background: scrub the guest once it has gone idle (M4c). Cheap and inert
    while vm_idle_scrub_seconds is 0. Started from the app lifespan.

    Its first pass also lists leftovers (leftovers.py), read only, and logs
    the count: after one interval, so the service boxes startup re-creates
    are registered by then and not miscounted."""
    first = True
    while True:
        try:
            await asyncio.sleep(settings.vm_reaper_interval_seconds)
            if first:
                first = False
                from . import leftovers
                try:
                    print(leftovers.summary_line(await leftovers.scan()))
                except Exception as e:  # noqa: BLE001 — advice only
                    print(f"[boxes] leftover scan skipped: {e}")
            await vm.reap_if_idle()
            if settings.vm_boxes_enabled:
                await _boxes_mod.reap_idle()
            from . import boxlog
            await boxlog.watch_all()      # crashes and failed boots nobody reported
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reaper hiccup must never kill the loop
            pass
