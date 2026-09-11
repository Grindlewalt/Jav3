"""Pairing: a machine gets its credentials by being confirmed in a browser.

The property under test throughout is that the pasted command carries nothing
that, on its own, yields a credential. The code gets a stranger to a confirm
page; only the operator's yes plus the device secret issued to the claiming
process gets the pairing token and the Access secret out — and exactly once.
"""
import httpx
import pytest

from backend import cfaccess, pairing
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds

CF_ID = "f1a3d47d6b3f56e9e267b3a85de5aab0.access"
CF_SECRET = "c" * 64


@pytest.fixture(autouse=True)
def fresh():
    pairing.reset_for_tests()
    cfaccess.hide()
    yield
    pairing.reset_for_tests()
    cfaccess.hide()


# --- the state machine ----------------------------------------------------------

def test_a_code_is_readable_and_never_ambiguous():
    t = pairing.create("macbook")
    raw = t.code.replace("-", "")
    assert len(raw) == 8 and all(c in pairing.ALPHABET for c in raw)
    for bad in "0O1I":
        assert bad not in pairing.ALPHABET
    # dashes, spaces and case are decoration, not information
    assert pairing.normalize(t.code.lower().replace("-", " ")) == t.code
    assert pairing.normalize("ABCD-EFG") is None          # too short
    assert pairing.normalize("ABCD-EFG0") is None         # not in the alphabet


def test_the_full_happy_path_hands_the_credentials_over_exactly_once():
    t = pairing.create("macbook")
    assert t.state == "waiting"
    c = pairing.claim(t.code, name="macbook", hostname="mb.local",
                      platform="darwin", peer="10.0.0.9")
    assert c.state == "claimed" and c.device_secret
    assert c.public()["claim"]["hostname"] == "mb.local"
    assert "device_secret" not in c.public()
    # polling with the right secret sees "claimed" until the operator says yes
    assert pairing.poll(t.code, c.device_secret).state == "claimed"
    pairing.approve(t.code, by="operator")
    got = pairing.poll(t.code, c.device_secret)
    assert got.state == "approved"
    pairing.release(got)
    assert got.state == "released" and got.device_secret == ""
    # spent: the same secret no longer opens anything
    with pytest.raises(pairing.Unknown):
        pairing.poll(t.code, c.device_secret)


def test_a_wrong_device_secret_is_indistinguishable_from_a_wrong_code():
    t = pairing.create("mb")
    c = pairing.claim(t.code, name="mb")
    with pytest.raises(pairing.Unknown):
        pairing.poll(t.code, c.device_secret[:-1] + "x")
    with pytest.raises(pairing.Unknown):
        pairing.poll("ZZZZ-ZZZZ", c.device_secret)
    # and a code nobody has claimed has no secret to match, so it is Unknown
    # too rather than a distinguishable "not yet claimed"
    u = pairing.create("other")
    with pytest.raises(pairing.Unknown):
        pairing.poll(u.code, "anything")


def test_a_second_claim_is_refused_and_shown_to_the_operator():
    """The host cannot tell the operator's machine from an attacker who saw the
    code. It refuses the second and records it, and the operator — who knows
    where they are sitting — decides."""
    t = pairing.create("mb")
    pairing.claim(t.code, name="mb", hostname="real.local", peer="10.0.0.9")
    with pytest.raises(pairing.Contested):
        pairing.claim(t.code, name="mb", hostname="evil.local", peer="1.2.3.4")
    view = pairing.get(t.code).public()
    assert view["claim"]["hostname"] == "real.local"
    assert [c["hostname"] for c in view["contested"]] == ["evil.local"]


def test_approve_needs_a_claim_and_deny_works_at_any_point():
    t = pairing.create("mb")
    with pytest.raises(pairing.WrongState):
        pairing.approve(t.code)                    # nobody has claimed it
    pairing.deny(t.code)                           # cancelling an unclaimed code
    assert pairing.get(t.code).state == "denied"
    with pytest.raises(pairing.Unknown):        # a dead code looks dead
        pairing.claim(t.code, name="mb")
    t2 = pairing.create("mb")
    c = pairing.claim(t2.code, name="mb")
    pairing.deny(t2.code)
    assert pairing.poll(t2.code, c.device_secret).state == "denied"
    with pytest.raises(pairing.WrongState):
        pairing.approve(t2.code)


def test_a_code_dies_after_fifteen_minutes():
    t = pairing.create("mb", now=1000.0)
    assert pairing.get(t.code, now=1000.0 + pairing.TTL_SECONDS - 1) is not None
    assert pairing.get(t.code, now=1000.0 + pairing.TTL_SECONDS) is None
    with pytest.raises(pairing.Unknown):
        pairing.claim(t.code, name="mb", now=1000.0 + pairing.TTL_SECONDS)
    assert pairing.live(now=1000.0 + pairing.TTL_SECONDS) == []


def test_guessing_is_throttled_far_below_the_code_space():
    """32^8 codes. Sixty wrong guesses per quarter hour, host-wide, is the
    bound that matters; the per-peer one is a courtesy to the global budget."""
    now = 5000.0
    for i in range(pairing._WRONG_CODE_PER_PEER):
        pairing.throttle("1.2.3.4", now=now + i)
        pairing.note_wrong_code("1.2.3.4", now=now + i)
    with pytest.raises(pairing.TooMany):
        pairing.throttle("1.2.3.4", now=now + 20)
    # a different peer is still fine until the global budget goes
    pairing.throttle("5.6.7.8", now=now + 20)
    for i in range(pairing._WRONG_CODE_GLOBAL):
        pairing.note_wrong_code(f"peer-{i}", now=now + 30)
    with pytest.raises(pairing.TooMany):
        pairing.throttle("9.9.9.9", now=now + 31)
    # and the budget comes back once the window has passed
    pairing.throttle("9.9.9.9", now=now + 31 + pairing._WRONG_CODE_WINDOW)


def test_a_legitimate_client_can_poll_for_the_whole_window():
    """Every 3s for 15 minutes is 300 calls, and they must all get through —
    being cut off at 299 would be the throttle attacking the operator."""
    now = 9000.0
    for i in range(pairing.TTL_SECONDS // pairing.POLL_INTERVAL):
        pairing.throttle("10.0.0.9", now=now + i * pairing.POLL_INTERVAL)


# --- over HTTP -------------------------------------------------------------------

@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    cfaccess.set_token(CF_ID, CF_SECRET, ["jarvis.example"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(c):
    r = await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
    assert r.status_code == 200


async def test_the_machine_side_is_reachable_without_any_credential(client):
    """The operator creates a code (logged in); the machine claims and polls
    with nothing but that code; the operator confirms; the machine gets the
    token and the Access secret once."""
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")
    await _login(client)
    r = await client.post("/api/computeruse/enroll", json={"name": "macbook"})
    assert r.status_code == 200
    code = r.json()["code"]
    assert r.json()["confirm_path"] == f"/pair/{code}"
    # the operator-side routes need a session
    assert (await anon.get(f"/api/computeruse/enroll/{code}")).status_code == 401
    assert (await anon.post(f"/api/computeruse/enroll/{code}/approve")).status_code == 401

    # the machine downloads the client with the code alone
    r = await anon.get(f"/api/computeruse/pair/client.tar.gz?code={code}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert r.headers["cache-control"].startswith("no-store")
    assert (await anon.get("/api/computeruse/pair/client.tar.gz?code=ZZZZ-ZZZZ")
            ).status_code == 401

    r = await anon.post("/api/computeruse/pair/claim",
                        json={"code": code.lower(), "name": "macbook",
                              "hostname": "mb.local", "platform": "darwin"})
    assert r.status_code == 200, r.text
    secret = r.json()["device_secret"]
    assert r.json()["confirm_path"] == f"/pair/{code}"

    # the confirm page sees who claimed it, and never the device secret
    r = await client.get(f"/api/computeruse/enroll/{code}")
    assert r.json()["state"] == "claimed"
    assert r.json()["claim"]["hostname"] == "mb.local"
    assert "device_secret" not in r.text

    r = await anon.post("/api/computeruse/pair/poll",
                        json={"code": code, "device_secret": secret})
    assert r.json()["state"] == "claimed" and "token" not in r.json()

    r = await client.post(f"/api/computeruse/enroll/{code}/approve")
    assert r.status_code == 200 and r.json()["state"] == "approved"

    r = await anon.post("/api/computeruse/pair/poll",
                        json={"code": code, "device_secret": secret})
    body = r.json()
    assert body["state"] == "approved"
    assert body["cf_access_id"] == CF_ID and body["cf_access_secret"] == CF_SECRET
    assert len(body["token"]) > 20
    # ...once
    r = await anon.post("/api/computeruse/pair/poll",
                        json={"code": code, "device_secret": secret})
    assert r.status_code == 404
    # and the approval is on the record
    r = await client.get("/api/security/events")
    kinds = [e["kind"] for e in r.json()["events"]]
    assert "machine_paired" in kinds


async def test_a_wrong_secret_gets_nothing_even_after_approval(client):
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")
    await _login(client)
    code = (await client.post("/api/computeruse/enroll", json={"name": "mb"})).json()["code"]
    r = await anon.post("/api/computeruse/pair/claim", json={"code": code, "name": "mb"})
    secret = r.json()["device_secret"]
    await client.post(f"/api/computeruse/enroll/{code}/approve")
    r = await anon.post("/api/computeruse/pair/poll",
                        json={"code": code, "device_secret": secret[:-2] + "zz"})
    assert r.status_code == 404 and "token" not in r.text
    # the real claimant is unaffected by the attempt
    r = await anon.post("/api/computeruse/pair/poll",
                        json={"code": code, "device_secret": secret})
    assert r.json()["state"] == "approved"


async def test_a_second_claim_is_a_409_and_is_shown_on_the_ticket(client):
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")
    await _login(client)
    code = (await client.post("/api/computeruse/enroll", json={"name": "mb"})).json()["code"]
    assert (await anon.post("/api/computeruse/pair/claim",
                            json={"code": code, "hostname": "real"})).status_code == 200
    r = await anon.post("/api/computeruse/pair/claim",
                        json={"code": code, "hostname": "impostor"})
    assert r.status_code == 409
    view = (await client.get(f"/api/computeruse/enroll/{code}")).json()
    assert [c["hostname"] for c in view["contested"]] == ["impostor"]


async def test_the_deny_route_cancels_a_code_before_or_after_a_claim(client):
    await _login(client)
    code = (await client.post("/api/computeruse/enroll", json={"name": "mb"})).json()["code"]
    r = await client.post(f"/api/computeruse/enroll/{code}/deny")
    assert r.json()["state"] == "denied"
    assert (await client.post(f"/api/computeruse/enroll/{code}/approve")).status_code == 409


# --- the reveal window ----------------------------------------------------------

async def test_the_access_secret_is_not_readable_by_default(client):
    """The old GET returned it to any logged-in browser. Now the secret is
    blank until the Settings page opens the window on purpose."""
    await _login(client)
    r = await client.get("/api/computeruse/cfaccess")
    assert r.json()["configured"] and r.json()["client_id"] == CF_ID
    assert r.json()["secret"] == "" and r.json()["revealed_until"] is None
    assert CF_SECRET not in r.text


async def test_revealing_needs_the_word_and_is_recorded_and_closes(client):
    await _login(client)
    r = await client.post("/api/computeruse/cfaccess/reveal", json={"confirm": "yes"})
    assert r.status_code == 400
    assert (await client.get("/api/computeruse/cfaccess")).json()["secret"] == ""

    r = await client.post("/api/computeruse/cfaccess/reveal", json={"confirm": " Confirm "})
    assert r.status_code == 200 and r.json()["revealed_until"]
    r = await client.get("/api/computeruse/cfaccess")
    assert r.json()["secret"] == CF_SECRET and r.json()["revealed_until"]

    r = await client.get("/api/security/events")
    ev = [e for e in r.json()["events"] if e["kind"] == "cfaccess_revealed"]
    assert ev and ev[0]["severity"] == "warn" and "operator" in ev[0]["summary"]

    # closes on its own...
    assert cfaccess.revealed_until(now=cfaccess._reveal_until + 1) is None
    # ...and on request
    await client.post("/api/computeruse/cfaccess/hide")
    assert (await client.get("/api/computeruse/cfaccess")).json()["secret"] == ""


async def test_the_machine_side_is_throttled(client):
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")
    codes = 0
    for _ in range(pairing._WRONG_CODE_PER_PEER + 3):
        r = await anon.post("/api/computeruse/pair/claim",
                            json={"code": "ZZZZ-ZZZZ", "name": "x"})
        if r.status_code == 429:
            break
        assert r.status_code == 404
        codes += 1
    assert codes == pairing._WRONG_CODE_PER_PEER
