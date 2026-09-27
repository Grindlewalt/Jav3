"""Per-project LAN access (backend/lanaccess.py + the egress proxy decision)."""
import pytest

from backend import db as db_mod, lanaccess
from backend.vm import egress_proxy as ep

HOST_IP = "10.0.0.82"
DNS = {"nas.lan": ["10.0.0.50"], "ha.lan": ["10.0.0.60"], "sneaky.lan": ["10.0.0.82"],
       "meta.lan": ["169.254.169.254"], "mixed.lan": ["10.0.0.50", "8.8.8.8"],
       "public.example": ["93.184.216.34"]}


@pytest.fixture(autouse=True)
def fake_host(monkeypatch):
    monkeypatch.setattr(lanaccess, "_host_view",
                        lambda: (frozenset({HOST_IP, "10.201.0.1", "172.17.0.1"}),
                                 (__import__("ipaddress").ip_network("172.17.0.0/16"),)))
    monkeypatch.setattr(lanaccess, "host_names", lambda: {"jav3box", "jav3box.local"})
    monkeypatch.setattr(lanaccess, "_resolve4", _fake_resolve4)


def _fake_resolve4(h):
    import ipaddress
    try:
        return [str(ipaddress.IPv4Address(h))]     # a literal answers itself
    except ValueError:
        return list(DNS.get(h, []))


# --- validation ---------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    HOST_IP, f"{HOST_IP}:8000", f"{HOST_IP}/32",           # the host itself
    "127.0.0.1", "127.0.0.1:8000", "127.0.0.0/8",           # loopback
    "169.254.169.254", "169.254.0.0/16",                    # link-local / metadata
    "10.201.0.1", "10.201.0.1:8443", "10.201.3.0/30",       # box gateway / proxy
    "172.17.0.5",                                           # host-internal docker net
    "localhost", "foo.localhost", "metadata.google.internal", "jav3box.local",
    "sneaky.lan", "meta.lan",                               # names resolving to refused IPs
    "8.8.8.8", "8.8.8.8:443", "1.1.1.0/24", "100.64.0.1",   # public / CGNAT: not this path
    "public.example",
    "0.0.0.0", "224.0.0.1", "255.255.255.255",
    "::1", "[fd00::1]:80", "fe80::1",                       # IPv6 refused
    "10.0.0.60:0", "10.0.0.60:70000", "10.0.0.60:http",     # bad ports
    "*.lan", "10.0.0.0/33", "bad_name!",
])
def test_refused_entries(entry):
    with pytest.raises(lanaccess.LanError):
        lanaccess.parse_entry(entry)


def test_parse_forms():
    assert lanaccess.parse_entry("10.0.0.0/24") == {
        "kind": "cidr", "value": "10.0.0.0/24", "port": None, "text": "10.0.0.0/24"}
    # host bits are normalised away
    assert lanaccess.parse_entry("10.0.0.7/24")["text"] == "10.0.0.0/24"
    e = lanaccess.parse_entry(" 10.0.0.60:8123 ")
    assert (e["kind"], e["value"], e["port"], e["text"]) == ("ip", "10.0.0.60", 8123, "10.0.0.60:8123")
    e = lanaccess.parse_entry("NAS.lan:5000")
    assert (e["kind"], e["value"], e["port"]) == ("host", "nas.lan", 5000)
    assert lanaccess.parse_entry("192.168.1.0/24")["kind"] == "cidr"
    assert lanaccess.parse_entry("172.20.0.9")["kind"] == "ip"
    # an unresolvable name is kept (judged by its answers at connect time)
    assert lanaccess.parse_entry("printer.lan")["kind"] == "host"


def test_cidr_covering_the_host_is_accepted_but_host_is_carved_out():
    assert lanaccess.validate(["10.0.0.0/24", "10.0.0.0/24", ""]) == ["10.0.0.0/24"]
    assert lanaccess.refusal(HOST_IP)
    assert lanaccess.match(["10.0.0.0/24"], HOST_IP, 8000, HOST_IP) == "10.0.0.0/24"  # match alone...
    # ...but decide() refuses it first (see below)


def test_validate_rejects_list_with_a_bad_entry():
    with pytest.raises(lanaccess.LanError, match="host itself"):
        lanaccess.validate(["10.0.0.50", HOST_IP])


# --- storage + proxy decision --------------------------------------------------

@pytest.fixture
async def db(tmp_env):
    await db_mod.init_db()
    conn = await db_mod.get_db()
    await conn.execute("INSERT INTO projects (slug, name, path) VALUES ('home','home','h')")
    await conn.execute("INSERT INTO projects (slug, name, path) VALUES ('other','other','o')")
    await conn.commit()
    yield conn
    await conn.close()


def _att(slug="home", kind="project"):
    return {"project": slug, "op_id": None, "conversation_id": None, "box_id": "b1",
            "service_id": None, "kind": kind, "peer_ip": None, "peer_port": None}


async def _events(db):
    async with db.execute("SELECT kind, severity, summary FROM security_events "
                          "WHERE kind = 'lan_access_changed' ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_off_by_default_and_refused(db):
    assert await lanaccess.get(db, "home") == {"enabled": False, "allow": []}
    v, reason = await ep._authorize("10.0.0.60", "8123", _att())
    assert v == "deny"


async def test_save_refuses_host_and_public(db):
    res = await lanaccess.set_(db, "home", enabled=True, allow=[HOST_IP])
    assert not res["ok"] and "host itself" in res["error"]
    res = await lanaccess.set_(db, "home", enabled=True, allow=["8.8.8.8"])
    assert not res["ok"] and "RFC1918" in res["error"]
    assert (await lanaccess.set_(db, "nope", enabled=True, allow=[]))["ok"] is False
    assert (await lanaccess.set_(db, "__general__", enabled=True))["ok"] is False
    assert await _events(db) == []


async def test_on_allows_listed_refuses_rest(db):
    res = await lanaccess.set_(db, "home", enabled=True,
                               allow=["10.0.0.60:8123", "nas.lan", "10.0.0.0/24"],
                               actor="op")
    assert res["ok"] and res["enabled"]
    ev = await _events(db)
    assert len(ev) == 1 and "ON" in ev[0]["summary"] and ev[0]["severity"] == "warn"

    v, reason, pin = await ep._authorize_target("10.0.0.60", "8123", _att())
    assert (v, pin) == ("allow", "10.0.0.60") and reason.startswith("LAN: ")
    v, _, pin = await ep._authorize_target("nas.lan", "443", _att())
    assert (v, pin) == ("allow", "10.0.0.50")
    # not listed: another LAN range
    v, reason, pin = await ep._authorize_target("192.168.1.10", "80", _att())
    assert v == "deny" and pin is None and "LAN allowlist" in reason
    # the host's own IP, even though 10.0.0.0/24 covers it
    v, reason, _ = await ep._authorize_target(HOST_IP, "8000", _att())
    assert v == "deny" and "host itself" in reason
    # a listed-looking name that resolves to the host / metadata
    assert (await ep._authorize("sneaky.lan", "80", _att()))[0] == "deny"
    assert (await ep._authorize("meta.lan", "80", _att()))[0] == "deny"
    # mixed public + private answers fail closed
    assert (await ep._authorize("mixed.lan", "80", _att()))[0] == "deny"
    # loopback / box gateway never
    assert (await ep._authorize("127.0.0.1", "8000", _att()))[0] == "deny"
    assert (await ep._authorize("10.201.0.1", "8443", _att()))[0] == "deny"


async def test_port_scoped_entry(db):
    await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.60:8123"])
    assert (await ep._authorize("10.0.0.60", "8123", _att()))[0] == "allow"
    assert (await ep._authorize("10.0.0.60", "22", _att()))[0] == "deny"


async def test_other_project_and_services_unaffected(db):
    await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.0/24"])
    assert (await ep._authorize("10.0.0.60", "80", _att("other")))[0] == "deny"
    assert (await ep._authorize("10.0.0.60", "80", _att("home", "service")))[0] == "deny"
    assert (await ep._authorize("10.0.0.60", "80", _att(None, "shared")))[0] == "deny"
    # a shared-box turn attributed to the project gets its setting
    assert (await ep._authorize("10.0.0.60", "80", _att("home", "shared")))[0] == "allow"


async def test_turning_off_and_list_change_raise_events(db):
    await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.60"])
    await lanaccess.set_(db, "home", allow=["10.0.0.60", "10.0.0.50"])
    await lanaccess.set_(db, "home", allow=["10.0.0.60", "10.0.0.50"])   # no change
    await lanaccess.set_(db, "home", enabled=False)
    ev = await _events(db)
    assert len(ev) == 3
    assert "changed" in ev[1]["summary"] and "off" in ev[2]["summary"]
    assert (await ep._authorize("10.0.0.60", "80", _att()))[0] == "deny"


async def test_deny_list_beats_lan_allow(db):
    from backend import egress
    await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.0/24"])
    await egress.set_lists(db, "home", deny=["nas.lan"])
    assert (await ep._authorize("nas.lan", "80", _att()))[0] == "deny"


async def test_public_target_still_judged_by_domain_policy(db):
    await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.0/24"])
    v, reason = await ep._authorize("public.example", "443", _att())
    assert v == "deny" and not reason.startswith("LAN")


async def test_unbound_secret_not_injected_to_lan(db, monkeypatch):
    from backend import egress, secrets as secrets_mod
    monkeypatch.setattr(secrets_mod, "load", lambda: {"K": "v4lue"})
    monkeypatch.setattr(secrets_mod, "hosts_for", lambda n: [])
    await egress.grant_secret(db, "home", "K")
    out, refused = await ep.inject_secrets(db, "home", "10.0.0.60", "x {{secret:K}}",
                                           bound_only=True)
    assert "v4lue" not in out and refused == ["K"]


async def test_api_round_trip(tmp_env):
    import httpx
    from backend.auth import hash_password
    from backend.main import app

    await db_mod.init_db()
    conn = await db_mod.get_db()
    await conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                       ("operator", hash_password("pw")))
    await conn.execute("INSERT INTO projects(slug, name, path) VALUES ('home','h','/tmp/h')")
    await conn.commit()
    await conn.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.get("/api/egress/lan/home")).status_code == 401
        await c.post("/api/auth/login", json={"username": "operator", "password": "pw"})
        r = (await c.get("/api/egress/lan/home")).json()
        assert r["enabled"] is False and r["allow"] == [] and HOST_IP in r["host_ips"]
        r = await c.put("/api/egress/lan/home", json={"enabled": True, "allow": [HOST_IP]})
        assert r.status_code == 400 and "host itself" in r.text
        r = await c.put("/api/egress/lan/home",
                        json={"enabled": True, "allow": ["10.0.0.60:8123"]})
        assert r.status_code == 200 and r.json()["allow"] == ["10.0.0.60:8123"]
        ev = (await c.get("/api/security/events")).json()["events"]
        assert any(e["kind"] == "lan_access_changed" for e in ev)


# --- the host's Gitea is always refused -------------------------------------------

async def test_gitea_always_refused(db, monkeypatch):
    """Boxes never reach the host's Gitea: by its LAN IP (the host itself), by
    its configured name on any port, or on loopback — even with LAN access on
    and a range covering the host, and even for a name on the domain allowlist."""
    from backend import egress
    from backend.config import settings
    monkeypatch.setattr(settings, "gitea_url", "http://gitbox.lan:3000")
    monkeypatch.setattr(settings, "gitea_port", 3000)
    monkeypatch.setitem(DNS, "gitbox.lan", ["93.184.216.40"])   # even a public answer
    assert (await lanaccess.set_(db, "home", enabled=True, allow=["10.0.0.0/24"]))["ok"]
    await egress.allow_host(db, "home", "gitbox.lan")
    for host, port in ((HOST_IP, "3000"), ("gitbox.lan", "3000"), ("gitbox.lan", "443"),
                       ("127.0.0.1", "3000"), ("localhost", "3000"), ("10.201.0.1", "3000")):
        v, reason, pin = await ep._authorize_target(host, port, _att())
        assert v == "deny" and pin is None, (host, port, reason)
    v, reason, _ = await ep._authorize_target(HOST_IP, "3000", _att())
    assert "Gitea" in reason
    v, reason, _ = await ep._authorize_target("gitbox.lan", "443", _att())
    assert "Gitea" in reason
    # a shared box and a service get the same refusal
    for att in (_att(None, "shared"), _att("home", "service")):
        assert (await ep._authorize_target("gitbox.lan", "3000", att))[0] == "deny"
    # other LAN hosts on the same port are not caught by the Gitea rule
    assert (await ep._authorize_target("10.0.0.60", "3000", _att()))[0] == "allow"
