"""One Cloudflare Access token, in one place, reaching every machine.

The bug behind all of this: the same service token existed in the database, in
whatever the operator had typed into the set-up wizard, and in every desktop
client's own config. Rotating it in Cloudflare broke those one at a time and
silently — music stopped with a redirect nobody saw, and the set-up command went
on carrying a secret that no longer existed. What it looked like from outside
was an unexplained 403.
"""
import json

import pytest

from backend import cfaccess, computeruse as cu

TOKEN_ID = "f1a3d47d6b3f56e9e267b3a85de5aab0.access"
SECRET_A = "a" * 64
SECRET_B = "b" * 64


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the secret store at a throwaway file."""
    from backend import secrets as secretstore
    p = tmp_path / "secrets.json"
    monkeypatch.setattr(secretstore.settings, "secrets_path", p, raising=False)
    return p


# --- the store ---------------------------------------------------------------

def test_the_token_lands_in_the_0600_store_not_the_database(store):
    cfaccess.set_token(TOKEN_ID, SECRET_A, ["jarvis.example"])
    assert cfaccess.get() == (TOKEN_ID, SECRET_A)
    assert cfaccess.configured()
    # 0600 is the whole reason for putting it here rather than in SQLite
    assert store.stat().st_mode & 0o077 == 0


def test_it_is_covered_by_the_leak_detector(store):
    """Living in the secret store is not tidiness — it is what makes
    writes.apply_write refuse a file the agent tries to write the token into,
    and what keeps it out of anything fed back to the model."""
    from backend import secrets as secretstore
    cfaccess.set_token(TOKEN_ID, SECRET_A, ["jarvis.example"])
    assert secretstore.find_in_bytes(f"token = {SECRET_A}".encode())
    assert SECRET_A not in (secretstore.scrub(f"the secret is {SECRET_A}") or "")


def test_headers_are_empty_rather_than_half_built(store):
    """A deployment not behind Access must not have to special-case every call
    site, and half a credential is worse than none."""
    assert cfaccess.headers() == {}
    cfaccess.set_token(TOKEN_ID, SECRET_A)
    assert cfaccess.headers() == {
        "CF-Access-Client-Id": TOKEN_ID, "CF-Access-Client-Secret": SECRET_A}


def test_a_blank_secret_keeps_the_stored_one(store):
    """So the GUI can re-save the id, or re-bind hosts, without making the
    operator re-type a secret it deliberately never shows them."""
    cfaccess.set_token(TOKEN_ID, SECRET_A)
    cfaccess.set_token(TOKEN_ID, "", ["music.example"])
    assert cfaccess.get() == (TOKEN_ID, SECRET_A)
    assert cfaccess.hosts() == ["music.example"]


@pytest.mark.parametrize("bad", [
    "   ",                       # nothing at all
    "not-an-id",                 # missing the .access suffix
    SECRET_A,                    # the secret pasted into the id field
])
def test_a_client_id_that_cannot_work_is_refused_on_sight(store, bad):
    """Every one of these produces a login redirect rather than an error, which
    reads as 'wrong password' instead of 'wrong field'."""
    with pytest.raises(cfaccess.CFAccessError):
        cfaccess.set_token(bad, SECRET_A)


def test_a_token_pasted_with_its_header_name_still_works(store):
    """Copying out of a web console or a chat message brings the label along."""
    cfaccess.set_token(f"CF-Access-Client-Id: {TOKEN_ID}",
                       f"CF-Access-Client-Secret: {SECRET_A}")
    assert cfaccess.get() == (TOKEN_ID, SECRET_A)


def test_whitespace_inside_a_token_is_refused_not_silently_kept(store):
    """A secret with a newline in it fails as an auth error much later, in a
    place that says nothing about where it came from."""
    with pytest.raises(cfaccess.CFAccessError):
        cfaccess.set_token(TOKEN_ID, SECRET_A[:20] + " " + SECRET_A[20:])


# --- the live push -----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rotated_token_reaches_a_connected_machine(store):
    """The point of the whole feature: rotate once, and the machines that are
    online do not have to be visited."""
    cfaccess.set_token(TOKEN_ID, SECRET_B)
    sent = []

    async def send(raw):
        sent.append(json.loads(raw))

    cu.register(cu.Client(id="mac-1", name="mac", platform="darwin", send=send))
    try:
        assert await cu.broadcast_access_token() == ["mac"]
        assert sent == [{"config": {"cf_access_id": TOKEN_ID,
                                    "cf_access_secret": SECRET_B}}]
    finally:
        cu.unregister("mac-1")


@pytest.mark.asyncio
async def test_a_machine_that_cannot_be_reached_is_reported_not_assumed(store):
    """Silence here would tell the operator the rotation was complete when that
    machine is about to lock itself out on its next reconnect."""
    cfaccess.set_token(TOKEN_ID, SECRET_B)

    async def dead(raw):
        raise ConnectionResetError("gone")

    cu.register(cu.Client(id="pi-1", name="pi", platform="linux", send=dead))
    try:
        assert await cu.broadcast_access_token() == []
    finally:
        cu.unregister("pi-1")


@pytest.mark.asyncio
async def test_nothing_is_pushed_when_no_token_is_stored(store):
    """An unconfigured deployment must not push an empty pair and wipe a token
    the client already had."""
    async def send(raw):
        raise AssertionError(f"should not have sent {raw}")

    cu.register(cu.Client(id="mac-1", name="mac", platform="darwin", send=send))
    try:
        assert await cu.broadcast_access_token() == []
    finally:
        cu.unregister("mac-1")
