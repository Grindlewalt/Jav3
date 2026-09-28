"""`python -m backend.cli doctor [--json]`: where this install stands, stage by
stage, and the one command that moves each unfinished stage forward.

The installer's `--check` answers "can this machine host Jav3" before there is
a venv; this answers "how far did the install get" once there is one. Both are
written for a person AND for an installing agent (docs/AGENT-INSTALL.md): each
stage says who can do the fix — `agent` (a plain command as the service user)
or `human` (sudo, BIOS, a password or an API key only the person has).

Read-only: nothing here starts, writes or installs anything. Exit 0 when every
required stage is ok, 1 otherwise (optional stages never fail the run).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_DIR, settings

USAGE = "usage: python -m backend.cli doctor [--json]"


def unit_name() -> str:
    """jarvis.service, or jarvis-<name>.service for a named instance: the unit
    is named after its config dir exactly as install.sh names both."""
    d = CONFIG_DIR.name
    return f"{d}.service" if d.startswith("jarvis") else "jarvis.service"


def _stage(sid: str, title: str, ok: bool | None, detail: str = "", fix: str = "",
           who: str = "agent", optional: bool = False) -> dict:
    # ok None = not applicable here (skipped), never a failure
    return {"id": sid, "title": title, "ok": ok, "optional": optional,
            "detail": detail, "fix": "" if ok else fix, "who": "" if ok else who}


def _systemctl(*args: str) -> str:
    if not shutil.which("systemctl"):
        return ""
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip()


def _user() -> str:
    import getpass
    return getpass.getuser()


def _linger() -> bool | None:
    """None where there is no loginctl to ask (not a failure)."""
    if not shutil.which("loginctl"):
        return None
    try:
        r = subprocess.run(["loginctl", "show-user", _user(), "-p", "Linger", "--value"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() == "yes"


def _health(port: int) -> bool:
    import httpx
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


async def _db_facts() -> dict:
    from . import setup_api
    from .db import init_db
    await init_db()
    out = {"login": await setup_api.users_exist(), "profile": None}
    try:
        cur = (await setup_api.profile_state())["current"]
        out["profile"] = cur and cur["summary"]
    except Exception:          # noqa: BLE001 — advice only; a stage says unknown
        out["profile"] = None
    return out


def _provider() -> tuple[bool, str]:
    from . import providers
    try:
        pid = providers.default_provider()
        p = providers.provider(pid)
    except Exception as e:     # noqa: BLE001
        return False, f"no default provider ({type(e).__name__})"
    if providers.needs_key(p) and not providers.api_key(pid):
        return False, f"default provider {pid} has no API key"
    return True, f"default provider {pid}"


def stages() -> list[dict]:
    repo = settings.base_dir
    py = f"{repo}/.venv/bin/python"
    unit = unit_name()
    port = settings.lan_port
    out = []

    out.append(_stage("venv", "python venv + dependencies", True, sys.prefix))

    dist = Path(repo) / "frontend" / "dist" / "index.html"
    out.append(_stage("frontend", "web UI built", dist.exists(), str(dist.parent),
                      f"cd {repo}/frontend && npm ci && npm run build"))

    env = CONFIG_DIR / "env"
    out.append(_stage("config", "config file", env.exists(), str(env),
                      f"bash {repo}/scripts/install.sh"))

    unit_file = Path.home() / ".config" / "systemd" / "user" / unit
    active = _systemctl("is-active", unit) == "active"
    if not unit_file.exists():
        out.append(_stage("service", f"{unit} running", False, "unit not installed",
                          f"bash {repo}/scripts/install.sh"))
    else:
        out.append(_stage("service", f"{unit} running", active,
                          "active" if active else "installed, not running",
                          f"systemctl --user restart {unit}"))
        linger = _linger()
        out.append(_stage("linger", "service survives logout", linger,
                          "" if linger else "systemd linger is off",
                          f"sudo loginctl enable-linger {_user()}", who="human"))

    healthy = _health(port)
    out.append(_stage("health", f"answers on port {port}", healthy,
                      f"http://127.0.0.1:{port}/api/health",
                      f"journalctl --user -u {unit} -n 50   (then fix what it says)"))

    # the runtime agent turns run in: KVM (the shared box) and/or Docker
    kvm = os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)
    vsock = os.path.exists("/dev/vhost-vsock")
    from .vm.lifecycle import base_built
    image = base_built()
    docker_err = None
    if shutil.which(settings.docker_bin):
        from .docker_setup import check_docker
        docker_err = check_docker()
    else:
        docker_err = "docker is not installed"
    docker_ready = settings.docker_enabled and docker_err is None

    if kvm and vsock:
        out.append(_stage("image", "guest golden image (KVM)", image,
                          str(settings.vm_dir),
                          f"VM_DIR={settings.vm_dir} bash {repo}/vm/build_base.sh   (~10 min)"))
    else:
        missing = [x for x, ok in (("/dev/kvm", kvm), ("/dev/vhost-vsock", vsock)) if not ok]
        out.append(_stage("kvm", "KVM runtime", False if not docker_ready else None,
                          "missing " + ", ".join(missing)
                          + ("; Docker boxes carry agent turns instead" if docker_ready else ""),
                          f"bash {repo}/scripts/install.sh --check   (it prints the one "
                          "sudo command, or says BIOS)", who="human",
                          optional=docker_ready))
    out.append(_stage("docker", "Docker boxes", docker_ready if docker_err is None else
                      (None if kvm else False),
                      docker_err or ("enabled" if settings.docker_enabled else "available, not enabled"),
                      f"{py} -m backend.cli docker-setup" if docker_err is None else
                      "install Docker and add this user to the docker group, then: "
                      f"{py} -m backend.cli docker-setup",
                      who="agent" if docker_err is None else "human",
                      optional=bool(kvm and vsock)))

    facts = asyncio.run(_db_facts())
    out.append(_stage("login", "first login created", facts["login"], "",
                      f"{py} -m backend.cli setup   (or open the one-time /setup link "
                      "the installer printed)", who="human"))
    ok, detail = _provider()
    out.append(_stage("provider", "model provider with a key", ok, detail,
                      f"{py} -m backend.cli setup --provider-only   (the person types the key at "
                      "a hidden prompt; or Settings > Providers in the web UI)", who="human"))
    out.append(_stage("profile", "default security profile", bool(facts["profile"]),
                      facts["profile"] or "none yet",
                      f"{py} -m backend.cli setup --profile-only"))

    gitea_ok = None
    if settings.gitea_enabled:
        from .gitea_setup import _healthy
        gitea_ok = _healthy()
    out.append(_stage("gitea", "Gitea (agent pull requests)", gitea_ok if settings.gitea_enabled
                      else False,
                      "running" if gitea_ok else ("enabled, not answering" if settings.gitea_enabled
                                                  else "not set up"),
                      f"{py} -m backend.cli gitea-setup", optional=True))
    return out


def summary(st: list[dict]) -> dict:
    todo = [s for s in st if s["ok"] is False and not s["optional"]]
    return {"ready": not todo, "unit": unit_name(), "port": settings.lan_port,
            "config_dir": str(CONFIG_DIR), "state_dir": str(settings.state_dir),
            "repo_dir": str(settings.base_dir),
            "next": todo[0]["id"] if todo else None, "stages": st}


def run(args: list[str]) -> int:
    if any(a not in ("--json",) for a in args):
        print(USAGE, file=sys.stderr)
        return 64
    rep = summary(stages())
    if "--json" in args:
        print(json.dumps(rep, indent=1))
        return 0 if rep["ready"] else 1
    for s in rep["stages"]:
        mark = {True: "ok  ", False: "opt " if s["optional"] else "MISS", None: "--  "}[s["ok"]]
        print(f"  {mark}  {s['title']:<30} {s['detail']}")
        if s["fix"] and s["ok"] is False:
            print(f"        fix ({s['who']}): {s['fix']}")
    print("\nready." if rep["ready"] else f"\nnext: {rep['next']}")
    return 0 if rep["ready"] else 1
