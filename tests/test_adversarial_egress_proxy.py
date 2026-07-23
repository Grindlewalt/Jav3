"""Adversarial audit of backend/vm/egress_proxy.py (parse_target + inject_secrets).

Originally the test_GAP_* cases demonstrated real bypasses (red); the gaps were
fixed, so these now assert the secure behaviour and PASS as regression tests
(renamed test_FIXED_*). test_DEMO_* document accepted, deliberate behaviour.
Run: .venv/bin/python -m pytest tests/test_adversarial_egress_proxy.py -q
"""
import json

import pytest

from backend import db as db_mod, egress
from backend.config import settings
from backend.vm import egress_proxy as ep


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    # OTHER_KEY / STRIPE_KEY exist but are granted to NO project.
    settings.secrets_path.write_text(json.dumps({
        "GRANTED_KEY": "realsecretvalue1",
        "OTHER_KEY": "othersecretval2",
        "STRIPE_KEY": "sk_live_ABCDEF1234567890",
    }))
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


# --- GAP 1 (HIGH): inject_secrets fails OPEN when the project slug is falsy ----
# `_allowed` is: `if slug and not await egress.may_use_secret(...)`. When slug is
# None or "" the grant check is short-circuited, so EVERY known secret is injected
# with no grant at all. The proxy calls inject_secrets(db, ctx["project"], raw)
# and ctx["project"] defaults to None (egress._context) — e.g. any request the
# compromised guest emits before/outside a registered turn. The function's own
# docstring promises injection ONLY for granted secrets.

async def test_FIXED_inject_secrets_slug_none_fails_closed(db):
    text = "GET /x HTTP/1.1\r\nAuthorization: Bearer {{secret:OTHER_KEY}}\r\n\r\n"
    out, refused = await ep.inject_secrets(db, None, "github.com", text)
    # SECURE: no project context -> nothing injected.
    assert "othersecretval2" not in out, "ungranted secret leaked onto the wire"
    assert refused == ["OTHER_KEY"]


async def test_FIXED_inject_secrets_slug_empty_fails_closed(db):
    text = "GET /x HTTP/1.1\r\nAuthorization: Bearer {{secret:OTHER_KEY}}\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "", "github.com", text)
    assert "othersecretval2" not in out, "ungranted secret leaked onto the wire"
    assert refused == ["OTHER_KEY"]


# Control confirmation: with a real slug the grant boundary DOES hold.
async def test_CONTROL_inject_secrets_with_slug_refuses_ungranted(db):
    text = "X-Key: {{secret:OTHER_KEY}}"
    out, refused = await ep.inject_secrets(db, "proj", "github.com", text)
    assert "othersecretval2" not in out and refused == ["OTHER_KEY"]


# --- GAP 2 (LOW): parse_target crashes on a non-numeric port ------------------
# Contract: "(method, host, port) ... or None if unparseable." A guest-controlled
# absolute-form request line with a bad port makes urlsplit(...).port raise
# ValueError, which is NOT caught in parse_target OR handle_conn — the connection
# handler coroutine dies with an unhandled exception instead of returning 400.

def test_GAP_parse_target_nonnumeric_port_should_return_none_not_raise():
    # SECURE expectation: unparseable -> None. Currently raises ValueError.
    assert ep.parse_target(b"GET http://pypi.org:notaport/ HTTP/1.1\r\n\r\n") is None


# --- host binding is now enforced when set; unbound-grant is deliberate --------
# The proxy now respects a secret's host binding (secrets hosts_for): a BOUND key
# is refused for a non-matching host. An UNBOUND granted key is still injected to
# the project's allowed hosts — the grant is the operator's explicit authorization
# (bind hosts to restrict it). These two tests pin both halves of that contract.

async def test_FIXED_bound_secret_refused_for_nonmatching_host(db):
    settings.secrets_path.write_text(json.dumps(
        {"STRIPE_KEY": {"value": "sk_live_ABCDEF1234567890", "hosts": ["api.stripe.com"]}}))
    await egress.grant_secret(db, "proj", "STRIPE_KEY")
    text = "GET /collect?k={{secret:STRIPE_KEY}} HTTP/1.1\r\nHost: github.com\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "proj", "github.com", text)
    assert "sk_live_ABCDEF1234567890" not in out and refused == ["STRIPE_KEY"]


async def test_DEMO_unbound_granted_secret_injected_by_design(db):
    # STRIPE_KEY granted but bound to no host -> grant is the authorization.
    await egress.grant_secret(db, "proj", "STRIPE_KEY")
    text = "GET /x?k={{secret:STRIPE_KEY}} HTTP/1.1\r\nHost: github.com\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "proj", "github.com", text)
    assert "sk_live_ABCDEF1234567890" in out and refused == []


# --- DEMO 4 (LOW): a spoofed second Host header survives into the forwarded ----
# request. parse_target authorises on the FIRST Host header, but _handle_http
# rebuilds headers into a dict where the LAST Host wins. The TCP connection still
# goes to the authorised host (IP-level containment holds), but the forwarded
# Host header is attacker-chosen — a domain-fronting primitive through the proxy.
def test_DEMO_first_host_header_authorised_but_dict_keeps_last():
    parsed = ep.parse_target(
        b"POST /x HTTP/1.1\r\nHost: pypi.org\r\nHost: evil.com\r\n\r\n")
    assert parsed == ("POST", "pypi.org", "80")    # authorised as pypi.org
    # (the rebuilt httpx headers dict would carry Host: evil.com — see
    # _handle_http; connection target is still pypi.org's IP.)
