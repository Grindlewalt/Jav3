"""Pairing: a machine gets its credentials by being confirmed in a browser.

The property under test throughout is that the pasted command carries nothing
that, on its own, yields a credential. The code gets a stranger to a confirm
page; only the operator's yes plus the device secret issued to the claiming
process gets its credential out — and exactly once.
"""
import pytest

from backend import pairing


@pytest.fixture(autouse=True)
def fresh():
    pairing.reset_for_tests()
    yield
    pairing.reset_for_tests()


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
