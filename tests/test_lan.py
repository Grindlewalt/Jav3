"""LAN auto-config: service URLs derived from services_host, the server's own
LAN identity folded into the CSRF/media allowlists, and mDNS that never takes
the app down. No test touches the real network."""
import asyncio
import sys
import types

import httpx
import pytest

from backend import lan
from backend.auth import hash_password
from backend.config import Settings, settings
from backend.db import get_db, init_db
from backend.main import app


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_service_urls_derive_from_services_host():
    s = _s(services_host="10.9.8.7")
    assert s.searxng_url == "http://10.9.8.7:8080"
    assert s.voice_sidecar_url == "ws://10.9.8.7:8100/ws"
    assert s.voice_local_base_url == "http://10.9.8.7:11434/v1"
    assert "http://10.9.8.7:11436" in s.model_base_url_allowlist
    assert "http://localhost:11434" in s.model_base_url_allowlist


def test_default_is_localhost_not_a_private_ip():
    s = _s()
    assert s.services_host == "localhost"
    assert s.searxng_url == "http://localhost:8080"
    assert not any("10.0.0.58" in u for u in
                   [s.searxng_url, s.voice_sidecar_url, s.voice_local_base_url,
                    *s.model_base_url_allowlist])
    assert "atomosnas" not in s.media_hosts


def test_explicit_service_url_wins():
    s = _s(services_host="10.9.8.7", searxng_url="http://search.lan:9999",
           model_base_url_allowlist=["http://gpu.lan:11434"])
    assert s.searxng_url == "http://search.lan:9999"
    assert s.model_base_url_allowlist == ["http://gpu.lan:11434"]
    # the ones left unset still derive
    assert s.voice_sidecar_url == "ws://10.9.8.7:8100/ws"


def test_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("JARVIS_SERVICES_HOST", "10.1.1.1")
    monkeypatch.setenv("JARVIS_VOICE_SIDECAR_URL", "ws://voice.lan:1/ws")
    s = _s()
    assert s.voice_sidecar_url == "ws://voice.lan:1/ws"
    assert s.searxng_url == "http://10.1.1.1:8080"


@pytest.fixture
def fake_lan(monkeypatch):
    monkeypatch.setattr(lan, "lan_ips", lambda: ["192.168.5.20"])
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "boxy")
    monkeypatch.setattr(lan, "_own", None)
    monkeypatch.setitem(lan._state, "hostname", "jav3.local")
    yield
    lan._own = None


def test_csrf_origin_derivation(fake_lan, monkeypatch):
    from backend.auth import origin_allowed
    monkeypatch.setattr(settings, "csrf_allowed_hosts", ["Extra.Example", "proxy.lan:8443"])

    def ok(origin):
        return origin_allowed({"origin": origin, "host": "test"}, "http")
    # own identity only on the server's own port
    for h in ("jav3.local", "boxy", "boxy.local", "192.168.5.20"):
        assert ok(f"http://{h}:{settings.lan_port}"), h
        assert not ok(f"http://{h}:31337"), h
    # explicit list is additive; a bare entry is any port, host:port is exact
    assert ok("http://extra.example:1234")
    assert ok("http://proxy.lan:8443") and not ok("http://proxy.lan:8444")
    assert not ok("http://evil.example")


async def test_csrf_check_still_refuses_foreign_origin(fake_lan, tmp_env):
    from fastapi import HTTPException
    from starlette.requests import Request

    from backend.auth import require_same_origin

    def req(origin, host="test"):
        return Request({"type": "http", "headers": [
            (b"origin", origin.encode()), (b"host", host.encode())]})
    require_same_origin(req("http://jav3.local:8000"))    # own mDNS name
    require_same_origin(req("http://192.168.5.20:8000"))    # own LAN IP
    with pytest.raises(HTTPException):
        require_same_origin(req("http://evil.example"))


def test_media_hosts_include_own_names(fake_lan, monkeypatch):
    monkeypatch.setattr(settings, "media_hosts", ["i.imgur.com"])
    assert lan.media_hosts()[0] == "i.imgur.com"
    assert "jav3.local" in lan.media_hosts()


def test_instance_name(monkeypatch):
    monkeypatch.setattr(settings, "instance_name", "")
    assert lan.instance_name("Jav3") == "jav3"
    monkeypatch.setattr(settings, "instance_name", "My Box!")
    assert lan.instance_name("Jav3") == "my-box"
    monkeypatch.setattr(settings, "instance_name", "")
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "Pi-4.lan")
    assert lan.instance_name("") == "pi-4"


async def test_mdns_without_zeroconf_is_noop(fake_lan, monkeypatch):
    # an import failure (no zeroconf installed) must not raise
    monkeypatch.setitem(sys.modules, "zeroconf", None)
    monkeypatch.setattr(settings, "mdns", True)
    await lan.start("Jav3")
    assert lan._state["advertised"] is False and lan._state["error"]
    await lan.stop()


def _fake_zeroconf(monkeypatch, zc_cls):
    zc_asyncio = types.ModuleType("zeroconf.asyncio")
    zc_asyncio.AsyncZeroconf = zc_cls
    zc = types.ModuleType("zeroconf")
    zc.ServiceInfo = lambda *a, **k: {"a": a, **k}
    zc.asyncio = zc_asyncio
    monkeypatch.setitem(sys.modules, "zeroconf", zc)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", zc_asyncio)
    monkeypatch.setattr(settings, "mdns", True)
    monkeypatch.setattr(settings, "instance_name", "")


async def test_mdns_socket_failure_is_noop(fake_lan, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise OSError("no multicast")
    _fake_zeroconf(monkeypatch, Boom)
    await lan.start("Jav3")
    assert lan._state["advertised"] is False
    assert "no multicast" in lan._state["error"]
    assert lan._zc is None


async def test_mdns_registers_and_unregisters(fake_lan, monkeypatch):
    calls = []

    class FakeZC:
        async def async_register_service(self, info, **k):
            calls.append(("reg", info))
            return asyncio.sleep(0)       # zeroconf hands back an awaitable

        async def async_unregister_service(self, info):
            calls.append(("unreg", info))
            return asyncio.sleep(0)

        async def async_close(self):
            calls.append(("close", None))
    _fake_zeroconf(monkeypatch, FakeZC)
    await lan.start("Jav3")
    assert lan._state["advertised"] is True
    info = calls[0][1]
    assert info["a"] == ("_http._tcp.local.", "jav3._http._tcp.local.")
    assert info["server"] == "jav3.local." and info["port"] == settings.lan_port
    await lan.stop()
    assert [c[0] for c in calls] == ["reg", "unreg", "close"]


async def test_mdns_opt_out(fake_lan, monkeypatch):
    monkeypatch.setattr(settings, "mdns", False)
    await lan.start("Jav3")
    assert lan._state["advertised"] is False and lan._zc is None


async def test_api_lan_requires_auth_and_reports(tmp_env, fake_lan, monkeypatch):
    async def fake_reach(url, timeout=1.0):
        return "8080" in url
    monkeypatch.setattr(lan, "_reachable", fake_reach)
    await init_db()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("pw")))
        await db.commit()
    finally:
        await db.close()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        assert (await c.get("/api/lan")).status_code == 401
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        body = (await c.get("/api/lan")).json()
    assert body["ips"] == ["192.168.5.20"] and body["port"] == settings.lan_port
    svc = {r["service"]: r["reachable"] for r in body["services"]}
    assert svc == {"searxng": True, "voice_sidecar": False, "voice_local": False}
