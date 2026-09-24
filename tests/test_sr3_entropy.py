"""SR3 adversarial review — ENTROPY / TIMING / ORACLES on paste-code login.

Tests named `test_poc_*` DEMONSTRATE a finding (they pass while the weakness
exists). Everything else pins a property that held up under attack. Timing
harnesses print medians (run with -s to see them) and only assert generous
bounds: on this laptop anything under a few hundred µs is scheduler noise.
"""
import base64
import collections
import importlib.machinery
import importlib.util
import logging
import random
import re
import statistics
import string
import time
from pathlib import Path

import httpx
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from backend import devicetokens, pastelogin
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app

LOGIN = "/api/devices/login"
MINT = "/api/devices/login-code"
URLSAFE = set(string.ascii_letters + string.digits + "-_")
CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("jav3cli_sr3", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_sr3", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load_cli()


@pytest.fixture(autouse=True)
def _reset():
    pastelogin.reset_for_tests()
    yield
    pastelogin.reset_for_tests()


async def _operator():
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()


@pytest.fixture
async def clients(tmp_env):
    await _operator()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as op, \
               httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as dev:
        await op.post("/api/auth/login", json={"username": "operator", "password": "hunter2"})
        yield op, dev


def _unthrottle():
    pastelogin._wrong.clear()
    pastelogin._calls.clear()


# =============================================================================
# 1. Entropy of the code and the device token
# =============================================================================

def test_2000_codes_length_alphabet_no_repeats():
    codes = [pastelogin.mint(by="op")[0] for _ in range(2000)]
    assert {len(c) for c in codes} == {43}
    assert set("".join(codes)) <= URLSAFE
    assert len(set(codes)) == 2000
    # 43 base64url chars have room for 258 bits; token_urlsafe(32) fills 256,
    # so the LAST char only ever carries 4 bits (one of 16 symbols). Proves the
    # encoder is fed exactly 32 bytes — no truncation, no short read.
    assert {c[-1] for c in codes} <= set("AEIMQUYcgkosw048")
    assert {len(base64.urlsafe_b64decode(c + "=")) for c in codes} == {32}
    # crude uniformity over the first 42 symbols (6 bits each): 2000*42/64
    # ≈ 1312 per symbol; a biased or reduced alphabet falls far outside ±25%
    counts = collections.Counter("".join(c[:42] for c in codes))
    assert len(counts) == 64
    exp = 2000 * 42 / 64
    assert all(0.75 * exp < n < 1.25 * exp for n in counts.values())
    # no shared 16-char prefixes (a seeded/looping generator would collide)
    assert len({c[:16] for c in codes}) == 2000


def test_code_comes_straight_from_os_urandom(monkeypatch):
    """Pin the RNG: patch the one urandom that secrets.SystemRandom reads and
    show the code IS base64url(those 32 bytes) — no fallback RNG, no mixing
    with time/pid, no truncation."""
    fixed = bytes(range(32))
    seen = []

    def fake(n):
        seen.append(n)
        return fixed[:n]

    monkeypatch.setattr(random, "_urandom", fake)
    code, _ = pastelogin.mint(by="op")
    assert seen == [32]
    assert code == base64.urlsafe_b64encode(fixed).rstrip(b"=").decode()


async def test_device_token_entropy_and_hash_only_storage(tmp_env):
    await init_db()
    toks = [(await devicetokens.mint("d"))[0] for _ in range(200)]
    assert {len(t) for t in toks} == {4 + 43}
    assert all(t.startswith("jvd_") and set(t[4:]) <= URLSAFE for t in toks)
    assert len(set(toks)) == 200
    db = await get_db()
    try:
        async with db.execute("SELECT * FROM device_tokens") as cur:
            dump = repr([tuple(r) for r in await cur.fetchall()])
    finally:
        await db.close()
    assert not any(t in dump or t[4:] in dump for t in toks)


def test_cli_parser_never_trims_a_code():
    """The `address=… code=…` parser: 2000 real codes, wrapped the ways a
    paste arrives (quotes, CRLF, tabs, reordered), come back byte-identical."""
    for i in range(2000):
        code = pastelogin._secrets.token_urlsafe(32)
        line = [f"address=h:8000 code={code}",
                f"  'address=h:8000 code={code}'\r\n",
                f"\tcode={code}\taddress=h:8000 ",
                f'"address=h:8000 code={code}"'][i % 4]
        assert jav3.parse_login_line(line) == ("h:8000", code)


def test_redeem_regex_accepts_every_real_code():
    """_CODE_RE must never reject a genuine code (e.g. one starting with '-')."""
    for _ in range(2000):
        assert pastelogin._CODE_RE.fullmatch(pastelogin._secrets.token_urlsafe(32))
    assert pastelogin._CODE_RE.fullmatch("-" + "A" * 42)


# =============================================================================
# 2. Oracles: 401 bodies/headers, revoked-vs-unknown, logs, listings
# =============================================================================

def _shape(r: httpx.Response) -> tuple:
    hdrs = {k: v for k, v in r.headers.items() if k not in ("date", "content-length")}
    return r.status_code, r.content, tuple(sorted(hdrs.items()))


async def test_redeem_401_is_byte_identical_for_every_failure(clients, monkeypatch):
    op, dev = clients

    async def fresh():
        return (await op.post(MINT, json={})).json()["code"]

    unknown = await dev.post(LOGIN, json={"code": "A" * 43})
    malformed = await dev.post(LOGIN, json={"code": "short"})
    used_code = await fresh()
    assert (await dev.post(LOGIN, json={"code": used_code})).status_code == 200
    used = await dev.post(LOGIN, json={"code": used_code})
    exp_code = await fresh()
    real = pastelogin.time.time
    monkeypatch.setattr(pastelogin.time, "time", lambda: real() + pastelogin.TTL_SECONDS + 1)
    expired = await dev.post(LOGIN, json={"code": exp_code})
    monkeypatch.setattr(pastelogin.time, "time", real)
    shapes = {_shape(r) for r in (unknown, malformed, used, expired)}
    assert len(shapes) == 1, shapes
    assert unknown.status_code == 401
    assert "retry-after" not in unknown.headers


async def test_require_actor_401_identical_revoked_unknown_malformed(clients):
    op, dev = clients
    code = (await op.post(MINT, json={})).json()["code"]
    tok = (await dev.post(LOGIN, json={"code": code})).json()["token"]
    hdr = {"Authorization": f"Bearer {tok}"}
    assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 200
    assert (await dev.delete("/api/devices/self", headers=hdr)).status_code == 200
    rs = [await dev.get("/api/devices/whoami", headers={"Authorization": f"Bearer {t}"})
          for t in (tok, "jvd_" + "Z" * 43, "garbage")]
    rs.append(await dev.get("/api/devices/whoami"))
    assert len({_shape(r) for r in rs}) == 1
    assert rs[0].status_code == 401


async def test_code_and_token_never_logged_listed_or_evented(clients, caplog):
    op, dev = clients
    caplog.set_level(logging.DEBUG)
    code = (await op.post(MINT, json={})).json()["code"]
    r = await dev.post(LOGIN, json={"code": code, "hostname": "box"})
    token = r.json()["token"]
    listing = (await op.get("/api/devices")).text
    events = (await op.get("/api/security/events")).text
    db = await get_db()
    try:
        async with db.execute("SELECT kind, summary, detail FROM security_events") as cur:
            rows = repr([tuple(x) for x in await cur.fetchall()])
    finally:
        await db.close()
    assert "device_enrolled" in rows
    for blob in (listing, events, rows, caplog.text, repr(pastelogin._tickets)):
        assert code not in blob and token not in blob and token[4:] not in blob
    # the code travels in the JSON body, never a URL (uvicorn access log = path only)
    assert code not in str(r.request.url)


async def test_no_store_on_both_secret_bearing_responses(clients):
    op, dev = clients
    m = await op.post(MINT, json={})
    assert m.headers["cache-control"] == "no-store"
    r = await dev.post(LOGIN, json={"code": m.json()["code"]})
    assert r.headers["cache-control"] == "no-store"


async def test_poc_device_ids_are_sequential(clients):
    """INFO: device ids are INTEGER PRIMARY KEY, returned to the device in the
    redeem body and whoami. A device learns how many computers were ever
    enrolled; the id itself grants nothing (revoke is cookie-only / self-only)."""
    op, dev = clients
    ids = []
    for _ in range(3):
        code = (await op.post(MINT, json={})).json()["code"]
        ids.append((await dev.post(LOGIN, json={"code": code})).json()["device_id"])
    assert ids == [ids[0], ids[0] + 1, ids[0] + 2]


# =============================================================================
# 3. Timing: the three 401 paths, and revoked-vs-unknown bearer
# =============================================================================

def _median_us(xs):
    return statistics.median(xs) * 1e6


def test_timing_redeem_function_unknown_expired_used():
    """Unit level, no HTTP noise. All three misses take the same code path:
    sweep → regex → sha256 → dict.pop → None. Expired tickets are removed by
    the sweep at the top of redeem, so 'expired' IS 'unknown' by the time the
    lookup runs; 'used' was popped by the first redeem."""
    N = 3000
    t = {"unknown": [], "expired": [], "used": []}
    for _ in range(N):
        pastelogin.reset_for_tests()
        u = pastelogin._secrets.token_urlsafe(32)
        e, _ = pastelogin.mint(by="op", now=time.time() - pastelogin.TTL_SECONDS - 5)
        s, _ = pastelogin.mint(by="op")
        assert pastelogin.redeem(s) is not None
        order = [("unknown", u), ("expired", e), ("used", s)]
        random.shuffle(order)
        for k, c in order:
            t0 = time.perf_counter()
            assert pastelogin.redeem(c) is None
            t[k].append(time.perf_counter() - t0)
    med = {k: _median_us(v) for k, v in t.items()}
    print("\n[SR3] redeem() medians µs: " + ", ".join(f"{k}={v:.2f}" for k, v in med.items()))
    assert max(med.values()) - min(med.values()) < 50


async def test_timing_redeem_endpoint_unknown_expired_used(clients):
    op, dev = clients
    N = 150
    t = {"unknown": [], "expired": [], "used": []}
    for _ in range(N):
        _unthrottle()
        s = (await op.post(MINT, json={})).json()["code"]
        assert (await dev.post(LOGIN, json={"code": s})).status_code == 200
        e = (await op.post(MINT, json={})).json()["code"]
        for tk in pastelogin._tickets.values():      # age `e` out in place
            tk.expires = time.time() - 1
        u = pastelogin._secrets.token_urlsafe(32)
        order = [("unknown", u), ("expired", e), ("used", s)]
        random.shuffle(order)
        for k, c in order:
            t0 = time.perf_counter()
            r = await dev.post(LOGIN, json={"code": c})
            t[k].append(time.perf_counter() - t0)
            assert r.status_code == 401
    med = {k: _median_us(v) for k, v in t.items()}
    print("\n[SR3] POST /login 401 medians µs: " + ", ".join(f"{k}={v:.0f}" for k, v in med.items()))
    assert max(med.values()) - min(med.values()) < 500


async def test_timing_bearer_revoked_vs_unknown(clients):
    """Revoked and never-issued tokens are both `SELECT … WHERE token_hash=?
    AND revoked=0` → no row → same 401. The UNIQUE index finds the revoked row
    and filters it, a sub-µs difference buried under the per-request
    connection open. And the attacker must already hold the revoked token."""
    op, dev = clients
    code = (await op.post(MINT, json={})).json()["code"]
    tok = (await dev.post(LOGIN, json={"code": code})).json()["token"]
    await dev.delete("/api/devices/self", headers={"Authorization": f"Bearer {tok}"})
    N = 200
    t = {"revoked": [], "unknown": []}
    for _ in range(N):
        order = [("revoked", tok), ("unknown", "jvd_" + pastelogin._secrets.token_urlsafe(32))]
        random.shuffle(order)
        for k, c in order:
            t0 = time.perf_counter()
            r = await dev.get("/api/devices/whoami", headers={"Authorization": f"Bearer {c}"})
            t[k].append(time.perf_counter() - t0)
            assert r.status_code == 401
    med = {k: _median_us(v) for k, v in t.items()}
    print("\n[SR3] whoami 401 medians µs: " + ", ".join(f"{k}={v:.0f}" for k, v in med.items()))
    assert abs(med["revoked"] - med["unknown"]) < 500


# =============================================================================
# 4. Findings (PoCs)
# =============================================================================

async def test_poc_xff_from_loopback_rewrites_peer_and_bypasses_per_peer_throttle(tmp_env):
    """MEDIUM (conditional). `_peer` reads request.client.host and its comment
    promises forwarding headers are never trusted — but scripts/jarvis.service
    runs bare `uvicorn backend.main:app`, whose defaults are proxy_headers=True,
    forwarded_allow_ips=127.0.0.1. Any TCP peer on loopback (an SSH -L forward,
    a local reverse proxy/tunnel, any local process) gets request.client.host =
    its chosen X-Forwarded-For: a fresh per-peer miss budget per request, and a
    forged `peer` in the device_enrolled / login_failed security events. The
    existing test_wrong_codes_are_throttled_per_peer misses this because
    httpx.ASGITransport doesn't install uvicorn's middleware."""
    await _operator()
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")   # uvicorn default
    transport = httpx.ASGITransport(app=wrapped, client=("127.0.0.1", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://jav3.lan:8000") as c:
        statuses = []
        for i in range(pastelogin._WRONG_PER_PEER * 3):
            r = await c.post(LOGIN, json={"code": "B" * 43},
                             headers={"X-Forwarded-For": f"10.66.{i // 250}.{i % 250}"})
            statuses.append(r.status_code)
    assert statuses == [401] * len(statuses)            # never 429
    assert len(pastelogin._wrong) == len(statuses) + 1  # one bucket per forged IP, + '*'


def test_poc_global_miss_budget_locks_out_operator():
    """LOW (DoS, not an oracle). 50 source addresses × 10 misses fill
    _WRONG_GLOBAL (500), and `throttle` then refuses EVERY peer for the
    10-minute window — including the operator redeeming a fresh valid code.
    One LAN host can hold many IPv6 addresses; with the loopback-XFF PoC one
    local peer suffices. Guessing is hopeless at 256 bits; this is lockout."""
    now = time.time()
    for p in range(pastelogin._WRONG_GLOBAL // pastelogin._WRONG_PER_PEER):
        for _ in range(pastelogin._WRONG_PER_PEER):
            pastelogin.note_wrong(f"fe80::{p:x}", now)
    pastelogin.mint(by="operator", now=now)
    with pytest.raises(pastelogin.TooMany):
        pastelogin.throttle("192.168.1.50", now + 1)       # operator's laptop


async def test_poc_default_login_line_is_cleartext_http(clients):
    """MEDIUM (deployment). Unless cookie_secure is set, the login line's
    address has no scheme and the CLI turns bare host:port into http://. The
    code goes out in cleartext, the device token comes BACK in cleartext, and
    every later chat call carries it in cleartext. A passive LAN observer
    needs no entropy attack: sniff the redeem response and the token is
    theirs until revoked."""
    op, _ = clients
    m = (await op.post(MINT, json={})).json()
    assert "://" not in m["address"]
    assert jav3.base_url(m["address"]).startswith("http://")


def test_poc_no_server_side_cancel_for_a_shown_code():
    """LOW. Settings "Done" (and the countdown reaching 0) only clears React
    state; no route drops a ticket. A dismissed code — pasted into the wrong
    window, or kept by a clipboard manager via <Copy> — stays redeemable for
    the full TTL. The countdown also compares the browser clock to the
    server's absolute `expires_at`, so a skewed client hides a live code."""
    routes = [(set(getattr(r, "methods", None) or ()), getattr(r, "path", "")) for r in app.routes]
    assert not any("DELETE" in m and "login-code" in p for m, p in routes)


# =============================================================================
# 5. CLI hygiene
# =============================================================================

def test_cli_login_takes_nothing_on_argv():
    p = jav3.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["login", "address=h:1 code=" + "A" * 43])
    assert p.parse_args(["--server", "h:1", "login"]).cmd == "login"


def test_cli_reads_line_hidden_on_tty(monkeypatch):
    got = {}

    def fake_getpass(prompt):
        got["p"] = prompt
        return "x"

    monkeypatch.setattr(jav3.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(jav3.getpass, "getpass", fake_getpass)
    jav3._read_login_line()
    assert got["p"] == jav3.PROMPT


def test_cli_parse_error_does_not_echo_the_line():
    with pytest.raises(jav3.CliError) as ei:
        jav3.parse_login_line("code=" + "Q" * 43)        # no address → refused
    assert "Q" * 43 not in str(ei.value)


def test_cli_reads_no_secret_from_env():
    src = CLI.read_text()
    assert set(re.findall(r'environ\.get\("([A-Z_]+)"', src)) == {"XDG_CONFIG_HOME"}
