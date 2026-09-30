"""`python -m backend.cli docker-setup`: make the Docker box runtime ready.

The installer runs this (scripts/install.sh docker_step) so nobody has to set
Docker boxes up by hand. Idempotent — a re-run changes nothing that is already
right:

  1. check `docker info` works for this user (the daemon is up and the user may
     use it). Docker itself is NEVER installed from here: a package manager
     partial upgrade broke node on the operator's Arch box, so a missing Docker
     is reported with what to do, and the step is skipped.
  2. check `setfacl` (the per-box socket ACLs need it).
  3. build the box image from vm/docker unless an image built from this exact
     Dockerfile + entrypoint already exists (label jav3.src.sha), then tag the
     service and builder images from it.
  4. smoke-test it: as the box user, with no network, read the entrypoint.
  5. switch the runtime on in Jav3's env file: JARVIS_DOCKER_ENABLED and
     JARVIS_VM_BOXES_ENABLED (Docker boxes are boxes).

Flags: --dry-run (say what would happen), --rebuild (build even if current),
--yes (no questions; there are none today, accepted for the installer).
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import settings
from .gitea_setup import Plan, set_env

SRC = Path(__file__).resolve().parent.parent / "vm" / "docker"
LABEL = "jav3.src.sha"
USAGE = "usage: python -m backend.cli docker-setup [--dry-run] [--rebuild] [--yes]"
_FLAGS = {"--dry-run", "--rebuild", "--yes"}


def _docker(*args: str, capture: bool = True, timeout: int | None = 60) -> subprocess.CompletedProcess:
    return subprocess.run([settings.docker_bin, *args], capture_output=capture, text=True,
                          timeout=timeout)


def src_sha(src: Path = SRC) -> str:
    """What the image is built from: every file under vm/docker, in order."""
    h = hashlib.sha256()
    for p in sorted(x for x in src.rglob("*") if x.is_file()):
        h.update(str(p.relative_to(src)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _image_sha(ref: str) -> str | None:
    r = _docker("image", "inspect", "--format",
                "{{ index .Config.Labels \"" + LABEL + "\" }}", ref)
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def has_buildx() -> bool:
    """True when `docker buildx` exists, i.e. BuildKit can be asked for. A host
    without the plugin (Arch ships docker-buildx as a separate package) has the
    classic builder only, and DOCKER_BUILDKIT=1 there aborts the build."""
    try:
        return _docker("buildx", "version", timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def check_docker() -> str | None:
    """None when this user can drive a running daemon, else what is wrong."""
    if shutil.which(settings.docker_bin) is None:
        return ("Docker is not installed. Install Docker Engine for your OS "
                "(https://docs.docker.com/engine/install/), add yourself to the "
                "'docker' group, log in again, then re-run: "
                "python -m backend.cli docker-setup")
    try:
        r = _docker("info", "--format", "{{.ServerVersion}}", timeout=20)
    except subprocess.TimeoutExpired:
        return "the Docker daemon did not answer within 20 s"
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["docker info failed"]
        hint = (" — add yourself to the 'docker' group and log in again"
                if "permission denied" in err[0].lower() else "")
        return f"docker info failed: {err[0]}{hint}"
    return None


def ensure_image(plan: Plan, rebuild: bool) -> bool:
    want = src_sha()
    turn = settings.docker_image_turn
    have = _image_sha(turn)
    if have == want and not rebuild:
        print(f"  ok    {turn} is current (built from {want})")
    else:
        why = "rebuild asked" if rebuild else ("not built yet" if have is None
                                               else f"vm/docker changed ({have} -> {want})")
        plan.say(f"docker build -t {turn} {SRC}  ({why}; several minutes on a Pi)")
        if not plan.dry:
            # BuildKit when the plugin is there; else the classic builder (the
            # Dockerfile builds under both)
            kit = "1" if has_buildx() else "0"
            if kit == "0":
                print("  note  docker buildx is not installed: using the classic builder")
            r = subprocess.run(
                [settings.docker_bin, "build", "--label", f"{LABEL}={want}",
                 "-t", turn, str(SRC)],
                env={**os.environ, "DOCKER_BUILDKIT": kit}, text=True)
            if r.returncode != 0:
                print("  FAIL  docker build failed (output above). If apt could not "
                      "reach its mirrors, check the host's firewall lets Docker's "
                      "bridge forward traffic.")
                return False
    for ref in (settings.docker_image_svc, settings.docker_image_builder):
        if ref == turn:
            continue
        if not plan.dry and _image_sha(ref) == want:
            print(f"  ok    {ref} tagged")
            continue
        plan.say(f"docker tag {turn} {ref}")
        if not plan.dry and _docker("tag", turn, ref).returncode != 0:
            print(f"  FAIL  could not tag {ref}")
            return False
    return True


def smoke(plan: Plan) -> bool:
    plan.say(f"smoke test: a no-network container from {settings.docker_image_turn} runs python")
    if plan.dry:
        return True
    # as the box's own user, reading the real entrypoint: the first image ran
    # python fine as root yet every box died on an unreadable bootstrap.py
    r = _docker("run", "--rm", "--network", "none", "--user", "10001:10001",
                "--read-only", "--entrypoint", "python3", settings.docker_image_turn,
                "-c", "import tarfile; tarfile.data_filter; "
                "open('/usr/local/lib/jav3/bootstrap.py').read(); print('jav3-docker-ok')",
                timeout=120)
    if "jav3-docker-ok" not in r.stdout:
        print(f"  FAIL  smoke test: {(r.stderr or r.stdout).strip()[-300:]}")
        return False
    print("  ok    smoke test passed")
    return True


def run(args: list[str]) -> int:
    if any(a not in _FLAGS for a in args):
        print(USAGE)
        return 2
    plan = Plan("--dry-run" in args)
    print("Docker boxes")
    problem = check_docker()
    if problem:
        print(f"  skip  {problem}")
        return 1
    print("  ok    Docker daemon reachable")
    mem = _docker("info", "--format", "{{.MemoryLimit}}", timeout=20).stdout.strip()
    if mem == "false":
        from .vm.docker_runtime import NO_MEMORY_LIMIT
        print(f"  warn  {NO_MEMORY_LIMIT}")
    if shutil.which("setfacl") is None:
        print("  skip  setfacl not found: install your OS's 'acl' package, then re-run")
        return 1
    if not ensure_image(plan, "--rebuild" in args) or not smoke(plan):
        return 1
    set_env(plan, {"JARVIS_DOCKER_ENABLED": "true", "JARVIS_VM_BOXES_ENABLED": "true"},
            what="Docker")
    from .doctor import unit_name       # jarvis-<name> for a named instance
    print("done. Docker boxes are available (restart Jav3 to pick it up: "
          f"systemctl --user restart {unit_name().removesuffix('.service')}). "
          "A profile set to 'Runs in: own container' uses them.")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
