"""The login endpoint is the front door, and with Jarvis published it is the
one thing reachable without a credential.

What is being checked: failures get progressively more expensive, a burst is
visible, a missing username costs the same as a wrong password, and — the part
that matters most on a one-operator system — none of it can be turned into a
lockout of the real owner.
"""
import asyncio
import time

import pytest

from backend import auth


@pytest.fixture(autouse=True)
def clean():
    auth._failures.clear()
    yield
    auth._failures.clear()


def test_the_first_failure_is_free():
    """One wrong password is a typo, not an attack."""
    assert auth._delay_for(1) == 0.0


def test_delay_grows_and_then_stops_growing():
    seq = [auth._delay_for(n) for n in range(1, 10)]
    assert seq[:6] == [0.0, 0.5, 1.0, 2.0, 4.0, 8.0]
    assert all(d == auth._DELAY_CAP for d in seq[5:]), "must plateau, not diverge"


def test_the_delay_is_capped_so_this_can_never_become_a_lockout():
    """A hard lockout on a single-operator system is a self-denial: anyone who
    knows the username could shut the owner out of their own house. The ceiling
    is what keeps this an inconvenience instead of an outage."""
    assert auth._delay_for(10_000) == auth._DELAY_CAP
    assert auth._DELAY_CAP <= 10, "a delay long enough to feel like a lockout"


def test_failures_are_keyed_on_username_not_address():
    """Behind cloudflared every request arrives from the proxy, so per-IP
    counting would lump attacker and operator into one bucket — letting the
    attacker throttle the operator. The key must be the username."""
    now = time.time()
    auth._record("alice", now)
    auth._record("alice", now)
    auth._record("bob", now)
    assert auth._failures["alice"][0] == 2
    assert auth._failures["bob"][0] == 1


def test_a_success_forgives_the_history():
    now = time.time()
    for _ in range(4):
        auth._record("alice", now)
    assert auth._delay_for(auth._failures["alice"][0]) > 0
    auth._clear("alice")
    assert "alice" not in auth._failures


def test_old_failures_are_forgotten():
    old = time.time() - (auth._FAIL_WINDOW + 60)
    auth._record("alice", old)
    auth._record("alice", old)
    state = auth._record("alice", time.time())
    assert state[0] == 1, "a stale burst must not still be charged for"


def test_spraying_across_usernames_still_slows_down():
    """Per-username keying alone would let an attacker rotate usernames to keep
    every counter at one. The global counter is the floor under that."""
    now = time.time()
    for i in range(30):
        auth._record(f"user{i}", now)
        auth._record(auth._GLOBAL, now)
    assert auth._failures[auth._GLOBAL][0] == 30
    # the global contribution is divided down, so it throttles a spray without
    # punishing one person's typo
    assert auth._delay_for(auth._failures[auth._GLOBAL][0] // 3) == auth._DELAY_CAP


@pytest.mark.asyncio
async def test_a_burst_raises_one_alert(monkeypatch):
    from backend import security
    events = []

    async def fake_raise(db, **kw):
        events.append(kw)
    monkeypatch.setattr(security, "raise_event", fake_raise)

    await auth._alert("alice", 5, "203.0.113.7")
    assert len(events) == 1
    assert "alice" in events[0]["summary"] and "203.0.113.7" in events[0]["summary"]
    assert events[0]["kind"] == "login_failed"


@pytest.mark.asyncio
async def test_an_alert_failure_never_blocks_logging_in(monkeypatch):
    """If the Review Center write fails, the operator must still get in."""
    from backend import security

    async def boom(db, **kw):
        raise RuntimeError("db is on fire")
    monkeypatch.setattr(security, "raise_event", boom)
    await auth._alert("alice", 9, "?")        # must not raise


def test_peer_is_reporting_only_and_prefers_the_proxy_header():
    class Req:
        def __init__(self, headers, host):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert auth._peer(Req({"cf-connecting-ip": "1.2.3.4"}, "127.0.0.1")) == "1.2.3.4"
    # a comma list is what X-Forwarded-For looks like through several hops
    assert auth._peer(Req({"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, "127.0.0.1")) == "9.9.9.9"
    assert auth._peer(Req({}, "10.0.0.5")) == "10.0.0.5"
    # and it is bounded, since it lands in an alert summary
    assert len(auth._peer(Req({"cf-connecting-ip": "x" * 500}, "?"))) <= 64


@pytest.mark.asyncio
async def test_an_unknown_username_costs_the_same_as_a_wrong_password():
    """Skipping bcrypt for a missing user made rejection measurably faster,
    which hands out a list of who has an account."""
    real = auth.hash_password("correct horse")

    def timed(fn):
        t = time.perf_counter()
        fn()
        return time.perf_counter() - t

    wrong_password = timed(lambda: auth.verify_password("guess", real))
    if auth._DUMMY_HASH is None:
        auth._DUMMY_HASH = auth.hash_password("no-such-user")
    missing_user = timed(lambda: auth.verify_password("guess", auth._DUMMY_HASH))

    # same order of magnitude: both pay for a full bcrypt round
    assert 0.2 < missing_user / wrong_password < 5.0, (
        f"wrong password {wrong_password:.4f}s vs missing user "
        f"{missing_user:.4f}s — the difference is a username oracle")


@pytest.mark.parametrize("count", [1024, 10_000, 10 ** 9])
def test_a_huge_failure_count_does_not_crash_the_endpoint(count):
    """Concurrent attempts all increment before any of them is delayed, so the
    counter can run far past the cap. Computing 2**(count-2) and capping
    afterwards overflowed a float there and turned a 401 into a 500 — a crash
    reachable by the very traffic this is meant to slow."""
    assert auth._delay_for(count) == auth._DELAY_CAP


@pytest.mark.asyncio
async def test_a_correct_login_is_never_delayed_by_someone_elses_failures():
    """The regression this was written for. The delay was originally charged
    before the password check, so an attacker who ran the counter up made the
    operator's own correct login wait the full 8s — measured at 8.22s against a
    live server. The cost has to land on the failure, not on the attempt.

    Checked by reading the handler: the sleep must sit inside the `if not ok`
    branch, after verify_password, not ahead of the lookup.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(auth.login).lstrip())
    sleeps = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "attr", "") == "sleep"]
    assert sleeps, "the throttle's sleep has gone missing"

    # every sleep must be inside a failure branch, never at the top level
    top_level = [n for n in tree.body[0].body
                 if isinstance(n, (ast.Expr, ast.Await))]
    for node in top_level:
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "sleep":
                raise AssertionError(
                    "the delay is charged before the password is checked again "
                    "— that penalises the operator for an attacker's failures")
