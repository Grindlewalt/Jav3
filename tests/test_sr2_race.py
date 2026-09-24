"""SR2 (race / replay / state) adversarial review of the paste-code device
login (backend/pastelogin.py, devices_api.py, devicetokens.py, the jav3 CLI).

Two kinds of test live here:
- POC_*     began as a PoC asserting the weakness; the fixer (SF) inverted
            each to assert the fixed behaviour, keeping the name so the
            finding stays traceable.
- BLOCKED_* a hypothesis that was tried and is defended; kept as a
            regression guard."""
import ast
import asyncio
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
import stat
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
    """FIXED: opening the DB for the device_enrolled event is inside the try
    whose job is 'the audit line must not eat the token' — a SQLITE_BUSY there
    now costs the audit row, not the token: the CLI still gets its 200."""
    code = await _mint(op)
    real_get_db = devices_api.get_db

    async def flaky():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(devices_api, "get_db", flaky)
    async with _client("10.0.2.2", raise_app_exceptions=False) as dev:
        r = await dev.post(LOGIN, json={"code": code})
    monkeypatch.setattr(devices_api, "get_db", real_get_db)
    assert r.status_code == 200 and r.json()["token"].startswith("jvd_")
    assert await _live_tokens() == 1


# =============================================================================
# 3. revocation vs. an in-flight turn, token lifetime
# =============================================================================

async def test_POC_revoked_device_turn_keeps_running_and_streaming(op, monkeypatch):
    """FIXED: the live-turn registry records who started each turn; revoking
    a device token (Settings, or DELETE /self) cancels that token's running
    turns through the /stop path, so nothing runs after the revoke and the
    open stream settles on the interruption. The conversation carries the
    device id that opened it."""
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

        r = await op.delete(f"/api/devices/{tok['device_id']}")
        assert r.status_code == 200 and r.json()["stopped_turns"] == 1
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 401

        release.set()
        r = await asyncio.wait_for(post, 5)
        assert ran_after_revoke == []
        assert "done-after-revoke" not in r.text
        assert chat_mod.INTERRUPTED_MARKER in r.text
    db = await get_db()
    try:
        async with db.execute("SELECT device_id FROM conversations") as cur:
            assert [row[0] for row in await cur.fetchall()] == [tok["device_id"]]
    finally:
        await db.close()


async def test_POC_revoke_self_stops_own_turns(op, monkeypatch):
    """`jav3 logout` (DELETE /self) stops the token's own running turn too;
    the operator's own turns are untouched."""
    code = await _mint(op)
    async with _client("10.0.3.9") as dev:
        tok = (await dev.post(LOGIN, json={"code": code})).json()
        hdr = {"Authorization": f"Bearer {tok['token']}"}
        started, release = asyncio.Event(), asyncio.Event()

        async def turn(cid, system_prompt, history, tools=None, **kw):
            started.set()
            await release.wait()
            yield {"type": "final", "content": "fin"}
        monkeypatch.setattr(chat_mod, "guest_turn", turn)
        mine = asyncio.create_task(dev.post(
            "/api/chat", json={"message": "a", "confirm_peak": True}, headers=hdr))
        await asyncio.wait_for(started.wait(), 5)
        started.clear()
        ops = asyncio.create_task(op.post(
            "/api/chat", json={"message": "b", "confirm_peak": True}))
        await asyncio.wait_for(started.wait(), 5)
        r = await dev.delete("/api/devices/self", headers=hdr)
        assert r.json()["stopped_turns"] == 1
        release.set()
        assert chat_mod.INTERRUPTED_MARKER in (await asyncio.wait_for(mine, 5)).text
        assert "fin" in (await asyncio.wait_for(ops, 5)).text


async def test_POC_device_token_never_expires(op):
    """FIXED: a device token dies at expires_at (device_token_ttl_days) or
    after device_token_idle_days without use; both look like any other
    unknown token (one 401). last_used_at is touched at most once a minute."""
    code = await _mint(op)
    async with _client("10.0.3.2") as dev:
        tok = (await dev.post(LOGIN, json={"code": code})).json()
        hdr = {"Authorization": f"Bearer {tok['token']}"}

        async def sql(q, *a):
            db = await get_db()
            try:
                await db.execute(q, a)
                await db.commit()
            finally:
                await db.close()

        async def row():
            db = await get_db()
            try:
                async with db.execute("SELECT expires_at, last_used_at FROM "
                                      "device_tokens") as cur:
                    return tuple(await cur.fetchone())
            finally:
                await db.close()
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 200
        exp, used = await row()
        assert exp and used
        await sql("UPDATE device_tokens SET last_used_at = datetime('now','-30 seconds')")
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 200
        assert (await row())[1] < used or (await row())[1] != used   # not rewritten
        listing = (await op.get("/api/devices")).json()["devices"][0]
        assert {"expires_at", "last_used_at", "idle_expires_at"} <= set(listing)
        # idle
        await sql("UPDATE device_tokens SET last_used_at = datetime('now','-31 days')")
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 401
        # absolute expiry, even when used recently
        await sql("UPDATE device_tokens SET last_used_at = datetime('now'), "
                  "expires_at = datetime('now','-1 seconds')")
        assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 401
        assert (await op.get("/api/devices")).json()["devices"] == []


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
    """FIXED: the miss budgets gate misses only. After 50 addresses spend the
    global miss budget, further MISSES get 429 — but the operator's valid
    code, checked against the store first, still redeems."""
    good = await _mint(op)
    for i in range(pastelogin._WRONG_GLOBAL // pastelogin._WRONG_PER_PEER):
        async with _client(f"10.0.9.{i + 1}") as atk:
            for _ in range(pastelogin._WRONG_PER_PEER):
                assert (await atk.post(LOGIN, json={"code": "B" * 43})).status_code == 401
    async with _client("10.0.0.77") as laptop:
        assert (await laptop.post(LOGIN, json={"code": "B" * 43})).status_code == 429
        r = await laptop.post(LOGIN, json={"code": good})
    assert r.status_code == 200
    assert pastelogin.live_count() == 0


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


def test_POC_wall_clock_step_back_extends_code_life(monkeypatch):
    """FIXED: TTLs run on time.monotonic(). A wall-clock step (the Pi has no
    RTC) neither stretches a code's life nor matters at all."""
    mono = [1000.0]
    monkeypatch.setattr(pastelogin.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(pastelogin.time, "time", lambda: 100_000.0)
    code, _ = pastelogin.mint(by="op")
    mono[0] += pastelogin.TTL_SECONDS + 1                  # real time passed
    monkeypatch.setattr(pastelogin.time, "time", lambda: 100_000.0 - 3600)  # NTP step back
    assert pastelogin.redeem(code) is None


def test_POC_wall_clock_step_back_prolongs_throttle_lockout(monkeypatch):
    """FIXED: the miss windows are monotonic too — a backward wall-clock step
    does not keep a lockout alive past its window."""
    mono = [5000.0]
    monkeypatch.setattr(pastelogin.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(pastelogin.time, "time", lambda: 200_000.0)
    for _ in range(pastelogin._WRONG_PER_PEER):
        pastelogin.note_wrong("10.0.0.5")
    with pytest.raises(pastelogin.TooMany):
        pastelogin.throttle("10.0.0.5")
    mono[0] += pastelogin._WRONG_WINDOW + 5
    monkeypatch.setattr(pastelogin.time, "time", lambda: 200_000.0 - 3600)
    pastelogin.throttle("10.0.0.5")                         # window over: no raise


# =============================================================================
# 5. the live-code cap as eviction / oracle
# =============================================================================

async def test_POC_cap_eviction_is_global_not_per_user(tmp_env):
    """FIXED: the live-code cap is per user — bob minting MAX_LIVE codes
    evicts only bob's oldest, never alice's code in flight."""
    await init_db()
    await _seed_user("alice", "pw-a")
    await _seed_user("bob", "pw-b")
    async with _client("10.0.0.10") as a, _client("10.0.0.11") as b, \
            _client("10.0.4.1") as dev:
        await a.post("/api/auth/login", json={"username": "alice", "password": "pw-a"})
        await b.post("/api/auth/login", json={"username": "bob", "password": "pw-b"})
        alice_code = await _mint(a)
        for _ in range(pastelogin.MAX_LIVE + 3):
            await _mint(b)
        r = await dev.post(LOGIN, json={"code": alice_code})
        assert r.status_code == 200
    assert pastelogin.live_count() == pastelogin.MAX_LIVE      # bob's, capped


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
    """FIXED: the unit pins `--workers 1`, and the app refuses to start when
    WEB_CONCURRENCY asks for more than one process (the store, throttle,
    chat._active_turns and the bus are process-local)."""
    from backend.main import require_single_process
    unit = (ROOT / "scripts" / "jarvis.service").read_text()
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "--workers 1" in exec_line
    for ok in ({}, {"WEB_CONCURRENCY": ""}, {"WEB_CONCURRENCY": "1"}):
        require_single_process(ok)
    for bad in ("2", "8", "0", "auto"):
        with pytest.raises(RuntimeError):
            require_single_process({"WEB_CONCURRENCY": bad})


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
