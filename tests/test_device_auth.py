"""Device-authorization enrollment: the RFC 8628 flow (reusing pairing.py) mints
a revocable API token a CLI can use, kind-isolated from computer-use pairing, and
accepted only on the routers that opt into require_actor."""
import httpx
import pytest

from backend import devicetokens, pairing
from backend.auth import hash_password
from backend.db import get_db, init_db
from backend.main import app


@pytest.fixture(autouse=True)
def _reset_pairing():
    pairing.reset_for_tests()
    yield
    pairing.reset_for_tests()


@pytest.fixture
async def clients(tmp_env):
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    # op = logged-in operator (cookie jar); dev = a device with no session
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as op, \
               httpx.AsyncClient(transport=transport, base_url="http://t") as dev:
        await op.post("/api/auth/login",
                      json={"username": "operator", "password": "hunter2"})
        yield op, dev


# --- pairing kind isolation (unit) -------------------------------------------

def test_kind_isolation_blocks_cross_harvest():
    t = pairing.create("cli-1", kind="device")
    # a device code is invisible to the computer-use namespace
    assert pairing.get(t.code, kind="computeruse") is None
    with pytest.raises(pairing.Unknown):
        pairing.claim(t.code, kind="computeruse")
    # ...but claimable as a device
    claimed = pairing.claim(t.code, name="cli-1", kind="device")
    assert claimed.state == "claimed" and claimed.device_secret
    # and a computer-use approve/poll cannot touch it
    with pytest.raises(pairing.Unknown):
        pairing.approve(t.code, kind="computeruse")
    with pytest.raises(pairing.Unknown):
        pairing.poll(t.code, claimed.device_secret, kind="computeruse")


# --- token store (unit) -------------------------------------------------------

async def test_token_mint_verify_revoke(tmp_env):
    await init_db()
    raw, tid = await devicetokens.mint("cli", hostname="box", platform="linux")
    assert raw.startswith("jvd_")
    who = await devicetokens.verify(raw)
    assert who and who["device_id"] == tid and who["name"] == "cli"
    assert await devicetokens.verify("jvd_" + "x" * 40) is None   # wrong token
    assert await devicetokens.verify("not-a-token") is None       # bad format
    assert await devicetokens.revoke(tid) is True
    assert await devicetokens.verify(raw) is None                 # revoked → dead
    assert await devicetokens.revoke(tid) is False                # idempotent


# --- full HTTP flow -----------------------------------------------------------

async def test_enroll_to_token_happy_path_and_single_use(clients):
    op, dev = clients
    # operator enrolls
    code = (await op.post("/api/devices/enroll", json={"name": "my-cli"})
            ).json()["code"]
    # device (no session) claims
    claim = (await dev.post("/api/devices/pair/claim",
                            json={"code": code, "name": "my-cli",
                                  "hostname": "box", "platform": "linux"})).json()
    secret = claim["device_secret"]
    # poll before approval → pending, no token
    r = (await dev.post("/api/devices/pair/poll",
                        json={"code": code, "device_secret": secret})).json()
    assert r["state"] == "claimed" and "token" not in r
    # operator approves
    assert (await op.post(f"/api/devices/enroll/{code}/approve")).status_code == 200
    # poll after approval → the token, exactly once
    r = (await dev.post("/api/devices/pair/poll",
                        json={"code": code, "device_secret": secret})).json()
    token = r["token"]
    assert token.startswith("jvd_")
    # the ticket is spent: a replay gets nothing usable
    r2 = await dev.post("/api/devices/pair/poll",
                        json={"code": code, "device_secret": secret})
    assert "token" not in r2.json()

    # the token authenticates as a device on the actor surface
    who = await dev.get("/api/devices/whoami",
                        headers={"Authorization": f"Bearer {token}"})
    assert who.status_code == 200 and who.json()["is_device"] is True

    # it reaches a require_actor router (chat) ...
    conv = await dev.get("/api/conversations",
                         headers={"Authorization": f"Bearer {token}"})
    assert conv.status_code == 200
    # ... but NOT a cookie-only control-plane router (secrets)
    sec = await dev.get("/api/secrets",
                        headers={"Authorization": f"Bearer {token}"})
    assert sec.status_code == 401


def test_throttle_is_kind_isolated():
    # spend the device flow's global wrong-code budget
    peer = "1.2.3.4"
    for _ in range(pairing._WRONG_CODE_GLOBAL):
        pairing.note_wrong_code(peer, kind="device")
    with pytest.raises(pairing.TooMany):
        pairing.throttle(peer, kind="device")
    # the computer-use flow shares the store but NOT the budget — still open
    pairing.throttle(peer, kind="computeruse")     # must not raise


async def test_csrf_origin_check_on_operator_routes(clients):
    op, _ = clients
    # a same-site sibling / cross-origin POST carrying the operator cookie is refused
    bad = await op.post("/api/devices/enroll", json={"name": "x"},
                        headers={"Origin": "http://evil.example"})
    assert bad.status_code == 403
    # the SPA's own same-origin request (Origin host == Host) is allowed
    ok = await op.post("/api/devices/enroll", json={"name": "x"},
                       headers={"Origin": "http://t"})
    assert ok.status_code == 200


async def test_wrong_secret_and_revocation(clients):
    op, dev = clients
    code = (await op.post("/api/devices/enroll", json={"name": "cli"})).json()["code"]
    await dev.post("/api/devices/pair/claim", json={"code": code, "name": "cli"})
    # a poll with the wrong device secret is indistinguishable from a bad code
    bad = await dev.post("/api/devices/pair/poll",
                         json={"code": code, "device_secret": "wrong"})
    assert bad.status_code == 404
    # approve + collect, then revoke, then the token is dead everywhere
    await op.post(f"/api/devices/enroll/{code}/approve")
    claim2 = await dev.post("/api/devices/pair/claim", json={"code": code})
    # (already claimed → contested; re-claim refused) — get the token from a fresh run
    # so use the first device_secret path instead:
    # re-enroll cleanly for the revoke half
    code2 = (await op.post("/api/devices/enroll", json={"name": "cli2"})).json()["code"]
    s2 = (await dev.post("/api/devices/pair/claim",
                         json={"code": code2, "name": "cli2"})).json()["device_secret"]
    await op.post(f"/api/devices/enroll/{code2}/approve")
    token = (await dev.post("/api/devices/pair/poll",
                            json={"code": code2, "device_secret": s2})).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    tid = [d["id"] for d in (await op.get("/api/devices")).json()["devices"]][0]
    assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 200
    assert (await op.delete(f"/api/devices/{tid}")).status_code == 200
    assert (await dev.get("/api/devices/whoami", headers=hdr)).status_code == 401
