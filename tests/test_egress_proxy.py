"""Egress proxy core: request-target parsing and grant-checked secret injection.
(The socket/TLS plumbing is integration-tested live on the Pi.)"""
import json

import pytest

from backend import db as db_mod, egress
from backend.config import settings
from backend.vm import egress_proxy as ep


def test_parse_connect():
    assert ep.parse_target(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n") == \
        ("CONNECT", "api.github.com", "443")


def test_parse_absolute_form():
    assert ep.parse_target(b"GET http://pypi.org/simple/ HTTP/1.1\r\n\r\n") == \
        ("GET", "pypi.org", "80")


def test_parse_origin_form_with_host_header():
    head = b"POST /v1/x HTTP/1.1\r\nHost: api.example.com:8080\r\n\r\n"
    assert ep.parse_target(head) == ("POST", "api.example.com", "8080")


def test_parse_garbage():
    assert ep.parse_target(b"not an http request\r\n\r\n") is None


@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    settings.secrets_path.write_text(json.dumps(
        {"GRANTED_KEY": "realsecretvalue1", "OTHER_KEY": "othersecretval2"}))
    conn = await db_mod.get_db()
    yield conn
    await conn.close()


async def test_injects_only_granted_secret(db):
    await egress.grant_secret(db, "proj", "GRANTED_KEY")
    text = "GET /x HTTP/1.1\r\nAuthorization: Bearer {{secret:GRANTED_KEY}}\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "proj", text)
    assert "realsecretvalue1" in out and refused == []


async def test_refuses_ungranted_secret_and_leaves_placeholder(db):
    # OTHER_KEY exists but is NOT granted to this project
    text = "GET /x HTTP/1.1\r\nAuthorization: Bearer {{secret:OTHER_KEY}}\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "proj", text)
    assert "othersecretval2" not in out          # value never leaks
    assert "{{secret:OTHER_KEY}}" in out          # placeholder left intact
    assert refused == ["OTHER_KEY"]


async def test_unknown_secret_refused(db):
    text = "X-Key: {{secret:NOPE}}"
    out, refused = await ep.inject_secrets(db, "proj", text)
    assert refused == ["NOPE"] and "{{secret:NOPE}}" in out


async def test_no_placeholder_passthrough(db):
    text = "GET /x HTTP/1.1\r\nHost: pypi.org\r\n\r\n"
    out, refused = await ep.inject_secrets(db, "proj", text)
    assert out == text and refused == []
