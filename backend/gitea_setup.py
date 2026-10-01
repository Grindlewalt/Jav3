"""`python -m backend.cli gitea-setup`: install and configure Gitea on this host.

Idempotent — every step checks before it acts, so a re-run is safe:

  1. download the pinned static binary for this arch (never a package
     manager: see the pacman partial-upgrade incident) and verify its sha256
  2. write app.ini once (INSTALL_LOCK, no registration, sign-in to view,
     offline, SQLite, HTTP only, SSH off) under <state_dir>/gitea
  3. `gitea migrate`, then the operator's admin account (same username as
     Jav3) and the `jav3-agent` bot, with the binary's own admin CLI
  4. an API token for each, 0600 in Jav3's config dir (kept when still valid)
  5. a systemd --user unit, enabled and started; wait for it to answer
  6. the operator's password set through the API (never in argv)
  7. JARVIS_GITEA_* written to the env file; a repo for every project
  8. every enabled account (not the owner, the bot or a site admin) a read
     collaborator on every repo; `--access` runs only this step, against a
     Gitea that is already running

--dry-run prints the plan and changes nothing.
"""
import asyncio
import getpass
import hashlib
import os
import platform
import re
import secrets as pysecrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import CONFIG_DIR, ENV_FILE, settings

# The ONE place the version is pinned. Checksums are Gitea's published
# .sha256 files for these exact builds (dl.gitea.com/gitea/<v>/).
GITEA_VERSION = "1.27.3"
GITEA_SHA256 = {
    "amd64": "4da93c2c10b6980c359bcb86d5573ebfd7770e2e151756534edee24c8c12d971",
    "arm64": "04c086d36dba793546e331484a9da34571763efdfa77dc526cc98e0f10917e7b",
}
DOWNLOAD = "https://dl.gitea.com/gitea/{v}/gitea-{v}-linux-{a}"

# names Gitea refuses as a username (a subset: the ones an operator might use)
RESERVED = {"admin", "api", "assets", "attachments", "avatar", "explore", "help",
            "install", "issues", "login", "user", "org", "repo", "notifications",
            "pulls", "raw", "swagger", "debug", "error", "new", "-", "."}


def gitea_arch(machine: str | None = None) -> str:
    m = (machine or platform.machine()).lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    raise SystemExit(f"gitea-setup: unsupported architecture {m}")


def unit_name() -> str:
    return f"jav3-gitea-{settings.gitea_port}.service"


def paths() -> dict[str, Path]:
    root = Path(settings.gitea_dir)
    return {"root": root, "bin": root / "bin" / f"gitea-{GITEA_VERSION}",
            "ini": root / "custom" / "conf" / "app.ini", "data": root / "data",
            "unit": Path.home() / ".config" / "systemd" / "user" / unit_name()}


def app_ini(root: Path, port: int, root_url: str, run_user: str,
            secret_key: str, internal_token: str) -> str:
    host = re.sub(r"^https?://", "", root_url).split("/")[0].split(":")[0] or "localhost"
    return f"""; written by `python -m backend.cli gitea-setup` (Jav3). Safe to edit;
; a re-run never overwrites this file.
APP_NAME = Jav3 Git
RUN_MODE = prod
RUN_USER = {run_user}
WORK_PATH = {root}

[server]
PROTOCOL = http
HTTP_ADDR = 0.0.0.0
HTTP_PORT = {port}
ROOT_URL = {root_url.rstrip('/')}/
DOMAIN = {host}
DISABLE_SSH = true
START_SSH_SERVER = false
OFFLINE_MODE = true
LFS_START_SERVER = false
APP_DATA_PATH = {root}/data

[database]
DB_TYPE = sqlite3
PATH = {root}/data/gitea.db

[repository]
ROOT = {root}/repos
DEFAULT_BRANCH = main
DEFAULT_PRIVATE = private

[security]
INSTALL_LOCK = true
SECRET_KEY = {secret_key}
INTERNAL_TOKEN = {internal_token}

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = true
DEFAULT_ALLOW_CREATE_ORGANIZATION = false
ENABLE_NOTIFY_MAIL = false

[oauth2]
ENABLED = false

[openid]
ENABLE_OPENID_SIGNIN = false
ENABLE_OPENID_SIGNUP = false

[mailer]
ENABLED = false

[api]
ENABLE_SWAGGER = false

[migrations]
ALLOW_LOCALNETWORKS = false

[cron.update_checker]
ENABLED = false

[log]
MODE = console
LEVEL = warn
ROOT_PATH = {root}/log
"""


def unit_text(p: dict[str, Path]) -> str:
    return f"""[Unit]
Description=Gitea {GITEA_VERSION} for Jav3 (port {settings.gitea_port})
After=network-online.target

[Service]
WorkingDirectory={p['root']}
Environment=GITEA_WORK_DIR={p['root']}
ExecStart={p['root']}/bin/gitea web --config {p['ini']} --work-path {p['root']}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target
"""


class Plan:
    """Runs a step, or (dry run) only says what it would do."""
    def __init__(self, dry: bool):
        self.dry = dry

    def say(self, msg: str) -> None:
        print(("  would: " if self.dry else "  ") + msg)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_binary(plan: Plan, p: dict[str, Path], arch: str) -> None:
    want = GITEA_SHA256[arch]
    link = p["root"] / "bin" / "gitea"
    if p["bin"].exists() and _sha256(p["bin"]) == want:
        print(f"  ok    gitea {GITEA_VERSION} ({arch}) already installed")
    else:
        url = DOWNLOAD.format(v=GITEA_VERSION, a=arch)
        plan.say(f"download {url} and verify sha256 {want[:16]}…")
        if not plan.dry:
            p["bin"].parent.mkdir(parents=True, exist_ok=True)
            tmp = p["bin"].with_suffix(".part")
            with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
            got = _sha256(tmp)
            if got != want:
                tmp.unlink(missing_ok=True)
                raise SystemExit(f"gitea-setup: checksum mismatch ({got}) — refusing")
            tmp.chmod(0o755)
            tmp.rename(p["bin"])
    plan.say(f"link {link} -> {p['bin'].name}")
    if not plan.dry:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(p["bin"].name)


def _gitea(p: dict[str, Path], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(p["root"] / "bin" / "gitea"), *args, "--config", str(p["ini"]),
                           "--work-path", str(p["root"])],
                          capture_output=True, text=True, timeout=300,
                          env={**os.environ, "GITEA_WORK_DIR": str(p["root"])})


def ensure_ini(plan: Plan, p: dict[str, Path], root_url: str) -> None:
    if p["ini"].exists():
        print(f"  ok    {p['ini']} exists (kept)")
        return
    plan.say(f"write {p['ini']} (port {settings.gitea_port}, {root_url})")
    if plan.dry:
        return
    for sub in ("custom/conf", "data", "repos", "log"):
        (p["root"] / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(p["root"], 0o700)
    tok = _gitea(p, "generate", "secret", "INTERNAL_TOKEN")
    internal = tok.stdout.strip() if tok.returncode == 0 else pysecrets.token_urlsafe(48)
    fd = os.open(p["ini"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(app_ini(p["root"], settings.gitea_port, root_url, getpass.getuser(),
                        pysecrets.token_urlsafe(48), internal))


def _users(p) -> set[str]:
    r = _gitea(p, "admin", "user", "list")
    if r.returncode != 0:
        raise SystemExit(f"gitea-setup: user list failed: {r.stderr.strip()[:300]}")
    names = set()
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def ensure_user(plan: Plan, p, login: str, admin: bool, existing: set[str]) -> bool:
    """True when the account was created now (its password is random)."""
    if login in existing:
        print(f"  ok    Gitea user {login} exists")
        return False
    plan.say(f"create Gitea user {login}{' (admin)' if admin else ''}")
    if plan.dry:
        return True
    args = ["admin", "user", "create", "--username", login,
            "--email", f"{login}@localhost", "--random-password",
            "--must-change-password=false"]
    if admin:
        args.append("--admin")
    r = _gitea(p, *args)
    if r.returncode != 0:
        raise SystemExit(f"gitea-setup: creating {login} failed: {r.stderr.strip()[:300]}")
    return True


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.chmod(path, 0o600)


def _token_valid(tok_path: Path, login: str, running: bool) -> bool:
    try:
        tok = tok_path.read_text().strip()
    except OSError:
        return False
    if not tok:
        return False
    if not running:
        return True         # can't check yet; checked again after start
    try:
        r = httpx.get(f"http://127.0.0.1:{settings.gitea_port}/api/v1/user",
                      headers={"Authorization": f"token {tok}"}, timeout=10)
        return r.status_code == 200 and r.json().get("login") == login
    except (httpx.HTTPError, ValueError):
        return False


def ensure_token(plan: Plan, p, login: str, scopes: str, path: Path, running: bool) -> None:
    if _token_valid(path, login, running):
        print(f"  ok    token for {login} in {path}")
        return
    plan.say(f"mint an API token for {login} ({scopes}) -> {path} (0600)")
    if plan.dry:
        return
    r = _gitea(p, "admin", "user", "generate-access-token", "--username", login,
               "--token-name", f"jav3-{int(time.time())}", "--scopes", scopes, "--raw")
    tok = r.stdout.strip().splitlines()[-1].strip() if r.returncode == 0 and r.stdout.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{40}", tok):
        raise SystemExit(f"gitea-setup: token for {login} failed: {r.stderr.strip()[:300]}")
    _write_secret(path, tok)


def _systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True).returncode


def _healthy() -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{settings.gitea_port}/api/healthz",
                         timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_service(plan: Plan, p) -> None:
    text = unit_text(p)
    changed = not (p["unit"].exists() and p["unit"].read_text() == text)
    if not changed:
        print(f"  ok    {unit_name()} installed")
    else:
        plan.say(f"install {p['unit']}")
        if not plan.dry:
            p["unit"].parent.mkdir(parents=True, exist_ok=True)
            p["unit"].write_text(text)
            _systemctl("daemon-reload")
    plan.say(f"systemctl --user enable --now {unit_name()} (restarted when changed)")
    if plan.dry:
        return
    _systemctl("enable", unit_name())
    _systemctl("restart" if changed or not _healthy() else "start", unit_name())
    for _ in range(60):
        if _healthy():
            print(f"  ok    Gitea answers on 127.0.0.1:{settings.gitea_port}")
            return
        time.sleep(1)
    raise SystemExit(f"gitea-setup: Gitea did not come up — journalctl --user -u {unit_name()}")


def set_env(plan: Plan, values: dict[str, str], what: str = "Gitea") -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0] not in values]
    new = keep + [f"{k}={v}" for k, v in values.items()]
    if new == lines:
        print(f"  ok    {ENV_FILE} has the {what} settings")
        return
    plan.say(f"record {', '.join(values)} in {ENV_FILE}")
    if not plan.dry:
        _write_secret(ENV_FILE, "\n".join(new))


async def _operator_login(arg: str | None) -> str:
    if arg:
        return arg
    if settings.gitea_owner:
        return settings.gitea_owner
    from .db import get_db, init_db
    await init_db()
    db = await get_db()
    try:
        async with db.execute("SELECT username FROM users ORDER BY id LIMIT 1") as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    return row["username"] if row else getpass.getuser()


def _password(args: list[str], interactive: bool) -> tuple[str, bool]:
    """(password, chosen). Not chosen = random, saved 0600 for the operator."""
    if "--password-stdin" in args:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 8:
            raise SystemExit("gitea-setup: password must be at least 8 characters")
        return pw, True
    if interactive:
        print("  Gitea password for the operator (you can type your Jav3 password to reuse it)")
        pw = getpass.getpass("  password: ")
        if len(pw) >= 8 and getpass.getpass("  again: ") == pw:
            return pw, True
        print("  (empty, too short or not matching — a random one will be saved instead)")
    return pysecrets.token_urlsafe(18), False


async def _set_password(login: str, pw: str, must_change: bool) -> None:
    from . import gitea
    await gitea.api("PATCH", f"/admin/users/{login}", json_body={
        "login_name": login, "source_id": 0, "password": pw,
        "must_change_password": must_change})


async def _ensure_all_repos() -> None:
    from . import gitea
    d = settings.projects_dir
    for proj in sorted(d.iterdir()) if d.is_dir() else []:
        if (proj / "project.md").exists() and gitea._SLUG.match(proj.name):
            try:
                await gitea.ensure_repo(proj.name)
                print(f"  ok    repo {gitea.owner()}/{proj.name}")
            except Exception as e:        # one bad project must not stop setup
                print(f"  warn  repo {proj.name}: {gitea.scrub(str(e))}")


async def _ensure_access(dry: bool) -> None:
    """Step 8: read access for every enabled account on every repo. Existing
    grants are never lowered, so a write the operator chose stays."""
    from . import gitea
    try:
        r = await gitea.backfill_access(dry=dry)
    except Exception as e:              # Gitea down, a token refused: say so, don't trace
        print(f"  warn  repo access: {gitea.scrub(str(e))}")
        return
    who = ", ".join(r["accounts"])
    if not r["accounts"]:
        print(f"  ok    no enabled account besides {gitea.owner()} (owner) and "
              f"{gitea.bot_user()} (bot) and site admins: nobody to share with")
    elif not r["missing"]:
        print(f"  ok    {who} can read all {r['repos']} repos")
    for m in r["missing"]:
        print(f"  {'would: ' if dry else ''}grant read: {m['login']} -> "
              f"{gitea.owner()}/{m['repo']}")
    if r["missing"] and not dry:
        print(f"  ok    {len(r['granted'])} grants made for {who}")


def run_access(dry: bool) -> None:
    from . import gitea
    if not gitea.enabled():
        raise SystemExit("gitea-setup --access: Gitea is not set up here "
                         "(run gitea-setup without --access first)")
    if not _healthy():
        raise SystemExit(f"gitea-setup --access: Gitea is not answering on 127.0.0.1:"
                         f"{settings.gitea_port} — systemctl --user start {unit_name()}")
    print(f"== Gitea access: every enabled account reads every repo"
          f"{' (dry run)' if dry else ''}")
    asyncio.run(_ensure_access(dry))


USAGE = ("usage: python -m backend.cli gitea-setup [--dry-run] [--user NAME] [--port N] "
         "[--password-stdin] [--reset-password] [--yes] [--access]")
_FLAGS = {"--dry-run", "--password-stdin", "--reset-password", "--yes", "--access"}
_VALUED = {"--user", "--port"}


def run(args: list[str]) -> None:
    # anything unknown (e.g. --help) prints usage: it used to run a REAL setup
    i = 0
    while i < len(args):
        if args[i] in _VALUED and i + 1 < len(args):
            i += 2
        elif args[i] in _FLAGS:
            i += 1
        else:
            raise SystemExit(USAGE)
    dry = "--dry-run" in args
    if "--access" in args:
        return run_access(dry)
    plan = Plan(dry)
    user_arg = args[args.index("--user") + 1] if "--user" in args else None
    if "--port" in args:
        settings.gitea_port = int(args[args.index("--port") + 1])
    interactive = sys.stdin.isatty() and "--yes" not in args
    arch = gitea_arch()
    p = paths()
    from . import gitea
    login = asyncio.run(_operator_login(user_arg))
    if login.lower() in RESERVED or login == settings.gitea_bot_user:
        raise SystemExit(f"gitea-setup: {login!r} can't be a Gitea username — pass --user NAME")
    root_url = gitea.public_url()
    print(f"== Gitea {GITEA_VERSION} for {login} at {root_url}{' (dry run)' if dry else ''}")

    ensure_binary(plan, p, arch)
    ensure_ini(plan, p, root_url)
    plan.say("gitea migrate (create/upgrade the SQLite schema)")
    if not dry:
        r = _gitea(p, "migrate")
        if r.returncode != 0:
            raise SystemExit(f"gitea-setup: migrate failed: {r.stderr.strip()[:300]}")
    existing = set() if dry else _users(p)
    created = ensure_user(plan, p, login, True, existing)
    ensure_user(plan, p, settings.gitea_bot_user, False, existing)
    ensure_token(plan, p, login, "all", settings.gitea_admin_token_path, False)
    ensure_token(plan, p, settings.gitea_bot_user, "write:repository,read:user",
                 settings.gitea_bot_token_path, False)
    ensure_service(plan, p)
    if not dry:     # the tokens again, now that they can be checked live
        ensure_token(plan, p, login, "all", settings.gitea_admin_token_path, True)
        ensure_token(plan, p, settings.gitea_bot_user, "write:repository,read:user",
                     settings.gitea_bot_token_path, True)

    if created or "--reset-password" in args:
        pw, chosen = (("", True) if dry else _password(args, interactive))
        plan.say(f"set {login}'s Gitea password (API, not argv)")
        if not dry:
            # never must_change: Gitea then refuses the operator's own API
            # token until a browser sign-in, so repo creation 403'd (2026-09-27)
            asyncio.run(_set_password(login, pw, must_change=False))
            if not chosen:
                pw_file = CONFIG_DIR / "gitea-admin.password"
                _write_secret(pw_file, pw)
                print(f"  note  a random first password is in {pw_file} (0600); sign in "
                      "with it, change it in Gitea (Settings > Account), then delete the file")

    env = {"JARVIS_GITEA_ENABLED": "true", "JARVIS_GITEA_OWNER": login,
           "JARVIS_GITEA_PORT": str(settings.gitea_port)}
    if settings.gitea_url:
        env["JARVIS_GITEA_URL"] = settings.gitea_url
    set_env(plan, env)
    if dry:
        print("dry run: nothing was changed")
        return
    settings.gitea_enabled, settings.gitea_owner = True, login
    asyncio.run(_ensure_all_repos())
    asyncio.run(_ensure_access(False))
    print(f"done. Gitea: {root_url}  (restart Jav3 to pick it up: systemctl --user restart jarvis)")
