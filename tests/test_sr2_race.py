"""SR2 (race / replay / state) adversarial review of the paste-code device
login (backend/pastelogin.py, devices_api.py, devicetokens.py, the jav3 CLI).

Two kinds of test live here:
- POC_*     PASSES because the weakness is real (it asserts the bad
            behaviour). A fix should make it fail — flip it then.
- BLOCKED_* a hypothesis that was tried and is defended; kept as a
            regression guard.
No production code is changed by this file."""
import ast
import asyncio
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

from backend import chat as chat_mod
from backend import devices_api, devicetokens, pastelogin
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

ROOT = Path(__file__).resolve().parent.parent
LOGIN = "/api/devices/login"
MINT = "/api/devices/login-code"
BASE = "http://jav3.lan:8000"


@pytest.fixture(autouse=True)
def _reset():
    pastelogin.reset_for_tests()
    yield
    pastelogin.reset_for_tests()


async def _seed_user(name="operator", pw="hunter2"):
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         (name, hash_password(pw)))
        await db.commit()
    finally:
        await db.close()


def _client(peer="10.0.0.5", **kw):
    t = httpx.ASGITransport(app=app, client=(peer, 40000), **kw)
    return httpx.AsyncClient(transport=t, base_url=BASE)


@pytest.fixture
async def op(tmp_env):
    await init_db()
    await _seed_user()
    async with _client("10.0.0.5") as c:
        r = await c.post("/api/auth/login",
                         json={"username": "operator", "password": "hunter2"})
        assert r.status_code == 200
        yield c


async def _mint(op) -> str:
    r = await op.post(MINT, json={})
    assert r.status_code == 200, r.text
    return r.json()["code"]


async def _live_tokens() -> int:
    return len(await devicetokens.list_tokens())


# =============================================================================
# 1. concurrent redeem — the headline claim
# =============================================================================

async def test_BLOCKED_concurrent_redeem_real_app_even_with_slow_mint(op, monkeypatch):
    """32 parallel redeems of one code through the real ASGI app, from 32
    distinct peers, with the token mint slowed so every loser is scheduled
    while the winner is parked mid-await. Exactly one token results."""
    real = devicetokens.mint

    async def slow_mint(*a, **kw):
        await asyncio.sleep(0.05)
        return await real(*a, **kw)
    monkeypatch.setattr(devices_api.devicetokens, "mint", slow_mint)
    code = await _mint(op)
    clients = [_client(f"10.0.1.{i}") for i in range(32)]
    try:
        rs = await asyncio.gather(*(c.post(LOGIN, json={"code": code}) for c in clients))
    finally:
        for c in clients:
            await c.aclose()
    assert sorted(r.status_code for r in rs) == [200] + [401] * 31
    assert await _live_tokens() == 1


def test_BLOCKED_no_await_between_lookup_and_pop():
    """Structural: `redeem` is a plain def and the handler calls it before
    its first await, so there is no interleaving point between lookup and
    pop (throttle + redeem + note_wrong also run as one atomic slice).
    Guards against redeem going async or the audit moving above it."""
    assert not inspect.iscoroutinefunction(pastelogin.redeem)
    assert not inspect.iscoroutinefunction(pastelogin.throttle)
    src = textwrap.dedent(inspect.getsource(devices_api.redeem_login_code))
    fn = ast.parse(src).body[0]
    awaits, redeems = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.Await):
            awaits.append(node.lineno)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "redeem"):
            redeems.append(node.lineno)
    assert redeems and min(redeems) < min(awaits)


# =============================================================================
# 2. lifecycle after the pop: mint failing, audit failing
# =============================================================================

async def test_BLOCKED_mint_failure_after_pop_is_fail_closed(op, monkeypatch):
    """Token mint raises after the ticket is popped: 500, the code is spent,
    no token exists — the operator mints a new code. By design; no double
    mint is possible."""
    async def boom(*a, **kw):
        raise RuntimeError("database is locked")
    code = await _mint(op)
    monkeypatch.setattr(devices_api.devicetokens, "mint", boom)
    async with _client("10.0.2.1", raise_app_exceptions=False) as dev:
        assert (await dev.post(LOGIN, json={"code": code})).status_code == 500
        monkeypatch.setattr(devices_api.devicetokens, "mint", devicetokens.mint)
        assert (await dev.post(LOGIN, json={"code": code})).status_code == 401
    assert await _live_tokens() == 0


async def test_POC_audit_db_open_failure_orphans_a_live_token(op, monkeypatch):
    """devices_api.py:350 — `db = await get_db()` sits OUTSIDE the try whose
    except exists so 'the audit line must not eat the token'. If opening the
    DB for the security event fails (SQLITE_BUSY while parallel agents write),
    the handler 500s AFTER the token row is committed: code spent, CLI never
    sees the token, and a live operator-reach token nobody holds sits in
    Devices with no device_enrolled event."""
    code = await _mint(op)
    real_get_db = devices_api.get_db

    async def flaky():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(devices_api, "get_db", flaky)
    async with _client("10.0.2.2", raise_app_exceptions=False) as dev:
        r = await dev.post(LOGIN, json={"code": code})
    monkeypatch.setattr(devices_api, "get_db", real_get_db)
    assert r.status_code == 500
    assert await _live_tokens() == 1                       # minted, undelivered
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM security_events "
                              "WHERE kind='device_enrolled'") as cur:
            assert (await cur.fetchone())[0] == 0          # and unaudited
    finally:
        await db.close()


# =============================================================================
# 3. revocation vs. an in-flight turn, token lifetime
# =============================================================================

async def test_POC_revoked_device_turn_keeps_running_and_streaming(op, monkeypatch):
    """require_actor runs once, at request start; the chat turn is a detached
    task (chat.py:start_turn) and its SSE tail is already open. After the
    operator revokes the token, the device's turn still runs to completion
    (tool calls included) and the device still receives the reply on the open
    stream. Nothing ties /stop to revocation, and conversations carry no
    device id, so the operator can't find which turns the device started."""
    code = await _mint(op)
    async with _client("10.0.3.1") as dev:
        tok = (await dev.post(LOGIN, json={"code": code})).json()
        hdr = {"Authorization": f"Bearer {tok['token']}"}
        release, started = asyncio.Event(), asyncio.Event()
        ran_after_revoke = []

        async def turn(cid, system_prompt, history, tools=None, **kw):
            started.set()
            await release.wait()
            ran_after_revoke.append(True)   # a tool call would land here
            yield {"type": "final", "content": "done-after-revoke"}
        monkeypatch.setattr(chat_mod, "guest_turn", turn)
        post = asyncio.create_task(dev.post(
            "/api/chat", json={"message": "hi", "confirm_peak": True}, headers=hdr))
        await asyncio.wait_for(started.wait(), 5)

        assert (await op.delete(f"/api/devices/{tok['device_id']}")).status_code == 200
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 401

        release.set()
        r = await asyncio.wait_for(post, 5)
        assert ran_after_revoke == [True]
        assert r.status_code == 200 and "done-after-revoke" in r.text
    db = await get_db()
    try:
        async with db.execute("PRAGMA table_info(conversations)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
    finally:
        await db.close()
    assert not {"device_id", "actor", "started_by"} & cols


async def test_POC_device_token_never_expires(op):
    """devicetokens.verify checks only `revoked`; a device token has no TTL
    and no idle timeout (cookie JWTs expire after settings.jwt_ttl_hours).
    The default login address is plain http on the LAN, so the redeem
    response and every chat request carry the bearer in clear; one capture
    replays forever."""
    code = await _mint(op)
    async with _client("10.0.3.2") as dev:
        tok = (await dev.post(LOGIN, json={"code": code})).json()
        db = await get_db()
        try:
            await db.execute("UPDATE device_tokens SET created_at='2000-01-01 00:00:00', "
                             "last_seen='2000-01-01 00:00:00'")
            await db.commit()
        finally:
            await db.close()
        r = await dev.get("/api/devices/whoami",
                          headers={"Authorization": f"Bearer {tok['token']}"})
        assert r.status_code == 200


async def test_BLOCKED_login_response_replay(op):
    """The token is only ever in the one 200 (no-store); replaying the
    redeem request gets the generic 401 and no second token."""
    code = await _mint(op)
    async with _client("10.0.3.3") as dev:
        first = await dev.post(LOGIN, json={"code": code})
        again = await dev.post(LOGIN, json={"code": code})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert again.status_code == 401 and "token" not in again.text
    assert await _live_tokens() == 1


# =============================================================================
# 4. throttle state
# =============================================================================

async def test_POC_fifty_lan_addresses_lock_everyone_out_of_redeem(op):
    """_WRONG_GLOBAL=500 / _WRONG_PER_PEER=10: one LAN host that claims 50
    IPv4 addresses (static aliases / ARP — cheap on a home /24) spends the
    global miss budget in one burst; the operator's VALID fresh code then
    429s for the next 10 minutes, renewable indefinitely. 'Loose so a
    neighbour can't lock us out' only holds for a neighbour with <50 IPs."""
    good = await _mint(op)
    for i in range(pastelogin._WRONG_GLOBAL // pastelogin._WRONG_PER_PEER):
        async with _client(f"10.0.9.{i + 1}") as atk:
            for _ in range(pastelogin._WRONG_PER_PEER):
                assert (await atk.post(LOGIN, json={"code": "B" * 43})).status_code == 401
    async with _client("10.0.0.77") as laptop:
        r = await laptop.post(LOGIN, json={"code": good})
    assert r.status_code == 429
    # the code was not consumed (throttle runs before redeem) — DoS only
    assert pastelogin.live_count() == 1


async def test_BLOCKED_single_peer_cannot_drain_global_call_budget(op):
    """Hypothesis: throttle() bumps _calls['*'] before the per-peer check, so
    an over-budget peer keeps charging the global call budget. Blocked in
    practice: every failed redeem charges _wrong, and the _wrong check raises
    BEFORE the calls bump, so one peer charges '*' at most _WRONG_PER_PEER
    times per window. (throttle() in isolation IS drainable — see
    test_global_call_budget — but the handler never calls it that way.)"""
    async with _client("10.0.8.1") as atk:
        for _ in range(pastelogin._CALLS_GLOBAL + 5):
            await atk.post(LOGIN, json={"code": "C" * 43})
    hits = pastelogin._hits(pastelogin._calls, "*", pastelogin._CALLS_WINDOW, time.time())
    assert len(hits) <= pastelogin._WRONG_PER_PEER
    good = await _mint(op)
    async with _client("10.0.0.78") as laptop:
        assert (await laptop.post(LOGIN, json={"code": good})).status_code == 200


def test_POC_wall_clock_step_back_extends_code_life():
    """TTL uses time.time(). The Pi has no RTC; an NTP step backwards after
    mint keeps a code redeemable for TTL + the step. time.monotonic() would
    not."""
    code, _ = pastelogin.mint(by="op", now=100_000.0)
    # 10 min + 1 s of real time later, but the clock was stepped back 1 h
    assert pastelogin.redeem(
        code, now=100_000.0 + pastelogin.TTL_SECONDS + 1 - 3600) is not None


def test_POC_wall_clock_step_back_prolongs_throttle_lockout():
    """Same root cause: after a backward step every recorded miss is 'in the
    future', `now - h` is negative (< window), so a lockout lasts window +
    step. (A forward step conversely wipes every budget at once.)"""
    for _ in range(pastelogin._WRONG_PER_PEER):
        pastelogin.note_wrong("10.0.0.5", now=200_000.0)
    later = 200_000.0 + pastelogin._WRONG_WINDOW + 5 - 3600   # real time: window over
    with pytest.raises(pastelogin.TooMany):
        pastelogin.throttle("10.0.0.5", now=later)


# =============================================================================
# 5. the live-code cap as eviction / oracle
# =============================================================================

async def test_POC_cap_eviction_is_global_not_per_user(tmp_env):
    """MAX_LIVE=16 evicts the globally-oldest ticket regardless of `by`. With
    two accounts (backend.cli create-user allows several), bob minting 16
    codes silently kills alice's code in flight. Single-operator variant: a
    tab that re-mints (retry loop, double-click x16) evicts the code the
    operator is about to paste — generic 401, no hint why."""
    await init_db()
    await _seed_user("alice", "pw-a")
    await _seed_user("bob", "pw-b")
    async with _client("10.0.0.10") as a, _client("10.0.0.11") as b, \
            _client("10.0.4.1") as dev:
        await a.post("/api/auth/login", json={"username": "alice", "password": "pw-a"})
        await b.post("/api/auth/login", json={"username": "bob", "password": "pw-b"})
        alice_code = await _mint(a)
        for _ in range(pastelogin.MAX_LIVE):
            await _mint(b)
        r = await dev.post(LOGIN, json={"code": alice_code})
        assert r.status_code == 401


async def test_BLOCKED_eviction_gives_attacker_no_oracle(op):
    """An evicted code and a never-issued code get the same 401 body; the
    unauthenticated side has no count/listing endpoint, and only a cookie
    session (same-origin) can mint, so an attacker can neither drive nor
    observe eviction."""
    first = await _mint(op)
    for _ in range(pastelogin.MAX_LIVE):
        await _mint(op)
    async with _client("10.0.4.2") as dev:
        evicted = await dev.post(LOGIN, json={"code": first})
        never = await dev.post(LOGIN, json={"code": "Z" * 43})
        cross = await dev.post(MINT, json={}, headers={"Origin": "http://evil.example"})
    assert evicted.status_code == never.status_code == 401
    assert evicted.json() == never.json()
    assert cross.status_code == 401


# =============================================================================
# 6. multi-worker assumption
# =============================================================================

def test_POC_single_process_is_not_enforced():
    """Ticket store, throttle, chat._active_turns and the bus are all
    process-local. scripts/jarvis.service does not pin `--workers 1`, and
    uvicorn takes --workers from $WEB_CONCURRENCY, which a systemd --user
    unit inherits from the user manager's environment. Nothing at startup
    refuses a second worker. Simulated with two module instances: a code
    minted in worker A is dead in worker B (fail-closed, confusing), and
    each worker has its own miss budget (budget x N)."""
    unit = (ROOT / "scripts" / "jarvis.service").read_text()
    assert "--workers" not in unit
    spec = importlib.util.spec_from_file_location(
        "pastelogin_worker_b", ROOT / "backend" / "pastelogin.py")
    worker_b = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = worker_b       # @dataclass needs its module registered
    try:
        spec.loader.exec_module(worker_b)
    finally:
        del sys.modules[spec.name]
    code, _ = pastelogin.mint(by="op")
    assert worker_b.redeem(code) is None
    for _ in range(pastelogin._WRONG_PER_PEER):
        pastelogin.note_wrong("10.0.0.66")
    with pytest.raises(pastelogin.TooMany):
        pastelogin.throttle("10.0.0.66")
    worker_b.throttle("10.0.0.66")          # fresh budget in the other worker


# =============================================================================
# 7. the CLI: logout, credential writes
# =============================================================================

def _load_cli():
    path = ROOT / "clients" / "jav3cli" / "jav3"
    loader = importlib.machinery.SourceFileLoader("jav3cli_sr2", str(path))
    spec = importlib.util.spec_from_loader("jav3cli_sr2", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load_cli()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


def test_POC_logout_reports_success_when_revoke_failed(cfg, monkeypatch):
    """jav3:199 — the DELETE /api/devices/self status is never checked. A
    5xx (DB locked, a proxy 502) or a 401/404 from a wrong `--server` still
    prints 'logged out' and deletes the only local copy of the token while
    it stays live server-side; only a network error prints the 'revoke in
    Settings' hint."""
    jav3.save_credentials("jav3.lan:8000", "jvd_" + "x" * 43)
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(500, json={"detail": "database is locked"})
    real = httpx.Client
    monkeypatch.setattr(jav3.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = io.StringIO()
    rc = jav3.cmd_logout(jav3.build_parser().parse_args(["logout"]), out)
    assert seen == [("DELETE", "/api/devices/self")]
    assert rc == 0 and "logged out" in out.getvalue()
    assert "revoke" not in out.getvalue()
    assert jav3.load_credentials() is None


def test_POC_concurrent_logins_corrupt_the_credentials_file(cfg, monkeypatch):
    """jav3:54-63 — fixed temp name, O_TRUNC without O_EXCL or a lock. Two
    `jav3 login` runs (two terminals, a retry) open/truncate the SAME temp
    inode. The first rename moves that inode into place; the second writer
    still holds an fd on it, so its shorter write lands over the longer one's
    tail IN the live credentials.json, and its own os.replace then dies with
    an uncaught FileNotFoundError. Result: a torn file that load_credentials
    reads as 'not logged in', both codes spent, both tokens live server-side
    with no local copy."""
    real_write = os.write
    both_open = threading.Barrier(2)
    long_done = threading.Event()

    def racing_write(fd, data):
        both_open.wait(5)                    # both have open()+truncated
        if b"jvd_short" in data:
            long_done.wait(5)                # short writer goes second
        n = real_write(fd, data)
        if b"jvd_long" in data:
            long_done.set()
        return n
    monkeypatch.setattr(jav3.os, "write", racing_write)
    errs = []

    def run(addr, tok):
        try:
            jav3.save_credentials(addr, tok)
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    t1 = threading.Thread(target=run, args=("long-host.example:8000", "jvd_long" + "L" * 60))
    t2 = threading.Thread(target=run, args=("s:1", "jvd_short"))
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    assert [type(e) for e in errs] == [FileNotFoundError]
    raw = (cfg / "credentials.json").read_text()
    with pytest.raises(ValueError):
        json.loads(raw)
    assert jav3.load_credentials() is None


def test_POC_symlinked_config_dir_is_followed_and_chmodded(cfg, tmp_path):
    """O_NOFOLLOW guards only the temp file's final component. A `jav3` dir
    that is a symlink is followed by mkdir(exist_ok) and os.chmod: the target
    is re-moded 0700 and receives the token. Needs write access to
    $XDG_CONFIG_HOME (same user, or a shared XDG_CONFIG_HOME) — low."""
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    cfg.parent.mkdir(parents=True)
    cfg.symlink_to(target)
    jav3.save_credentials("h:1", "jvd_tok")
    assert (target / "credentials.json").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_BLOCKED_symlink_at_temp_name_refused(cfg, tmp_path):
    """The commit's claim holds: a symlink at credentials.json.tmp is not
    written through (ELOOP). Note the error surfaces as an uncaught OSError
    traceback AFTER the code was already spent server-side."""
    cfg.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (cfg / "credentials.json.tmp").symlink_to(victim)
    with pytest.raises(OSError):
        jav3.save_credentials("h:1", "jvd_tok")
    assert victim.read_text() == "keep"


def test_BLOCKED_symlink_at_final_name_is_replaced_not_followed(cfg, tmp_path):
    cfg.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim2"
    victim.write_text("keep")
    (cfg / "credentials.json").symlink_to(victim)
    jav3.save_credentials("h:1", "jvd_tok")
    assert victim.read_text() == "keep"
    assert not (cfg / "credentials.json").is_symlink()


def test_BLOCKED_cli_never_follows_redirects_with_token():
    c = jav3._client("http://x", "jvd_tok")
    try:
        assert c.follow_redirects is False
    finally:
        c.close()
