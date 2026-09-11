"""The client's --pair: credentials arrive by being confirmed in a browser.

What is being protected is the terminal. Before this the set-up command carried
the pairing token and the Cloudflare Access secret in plain text; now it carries
a fifteen-minute code, and the two secrets reach this machine only in the reply
to a poll made with a device secret nobody ever saw. So the tests are about
what the pasted command contains (nothing), what gets printed (never a secret),
and what each way of failing says.
"""
import importlib.util
import json
from pathlib import Path

import pytest

CLIENT_DIR = Path(__file__).resolve().parents[1] / "clients" / "computeruse"
TOKEN = "TePz_test_pairing_token_value_here_0123456789"
CF_ID = "f1a3d47d6b3f56e9e267b3a85de5aab0.access"
CF_SECRET = "9fada4ab86dc0a5ced63092006433f3326df2058048a2a22a44a66f3a1c427f6"
DEVICE = "device-secret-never-printed-xyz"


@pytest.fixture
def agent_mod():
    spec = importlib.util.spec_from_file_location("cu_agent_pair_t", CLIENT_DIR / "agent.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _server(script):
    """A fake Jarvis: `script` maps a path suffix to a list of replies, each
    (status, body-dict-or-None) consumed in order; the last one repeats."""
    calls = []

    def http(url, body=None, timeout=20):
        calls.append((url, body))
        for suffix, replies in script.items():
            if url.endswith(suffix):
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                status, parsed = reply
                return status, parsed, json.dumps(parsed or {}).encode()
        raise AssertionError(f"unexpected call {url}")
    return http, calls


def test_the_happy_path_saves_what_arrived_and_prints_none_of_it(
        agent_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    http, calls = _server({
        "/pair/claim": [(200, {"ok": True, "device_secret": DEVICE, "interval": 3,
                               "expires_in": 900, "confirm_path": "/pair/K7QX-4MRP"})],
        "/pair/poll": [(200, {"ok": True, "state": "claimed"}),
                       (200, {"ok": True, "state": "claimed"}),
                       (200, {"ok": True, "state": "approved", "token": TOKEN,
                              "cf_access_id": CF_ID, "cf_access_secret": CF_SECRET,
                              "name": "macbook"})],
    })
    monkeypatch.setattr(agent_mod, "_http_json", http)
    monkeypatch.setattr(agent_mod, "_ping", lambda *a, **k: (True, "reached it"))
    monkeypatch.setattr(agent_mod, "_selftest", lambda: 0)
    started = []

    async def fake_serve(self):
        started.append((self.name, self.token, self.cf_id, self.cf_secret))
        return 0
    monkeypatch.setattr(agent_mod.Agent, "serve", fake_serve)

    # the code is typed by a person: lower case and a missing dash are fine
    rc = agent_mod.main(["--pair", "k7qx-4mrp", "--server", "https://jarvis.example"])
    assert rc == 0
    # the claim carried the code and nothing secret; the polls carried the
    # device secret Jarvis issued, not anything typed
    assert calls[0][1]["code"] == "K7QX-4MRP" and "token" not in calls[0][1]
    assert all(c[1]["device_secret"] == DEVICE for c in calls[1:])
    # what arrived is what the service will run with
    assert started == [("macbook", TOKEN, CF_ID, CF_SECRET)]
    saved = json.loads((tmp_path / "jarvis" / "computeruse.json").read_text())
    assert saved["token"] == TOKEN and saved["cf_access_secret"] == CF_SECRET
    assert saved["name"] == "macbook"
    out = capsys.readouterr().out
    assert "https://jarvis.example/pair/K7QX-4MRP" in out, "the confirm link is the point"
    assert "confirmed" in out and "1/4" in out
    for secret in (TOKEN, CF_SECRET, DEVICE):
        assert secret not in out, "scrollback ends up in screenshots"


def test_a_name_given_here_beats_the_one_on_the_ticket(agent_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    http, _ = _server({
        "/pair/claim": [(200, {"ok": True, "device_secret": DEVICE})],
        "/pair/poll": [(200, {"ok": True, "state": "approved", "token": TOKEN,
                              "name": "from-the-tab"})],
    })
    monkeypatch.setattr(agent_mod, "_http_json", http)
    monkeypatch.setattr(agent_mod, "_ping", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(agent_mod, "_selftest", lambda: 0)

    async def fake_serve(self):
        return 0
    monkeypatch.setattr(agent_mod.Agent, "serve", fake_serve)
    assert agent_mod.main(["--pair", "K7QX-4MRP", "--server", "https://x",
                           "--name", "typed"]) == 0
    saved = json.loads((tmp_path / "jarvis" / "computeruse.json").read_text())
    assert saved["name"] == "typed"
    # not behind Access: no half-credential is written
    assert "cf_access_id" not in saved and "cf_access_secret" not in saved


@pytest.mark.parametrize("status, body, phrase", [
    (302, None, "Bypass policy"),                     # Access in the way
    (404, {"detail": "no such pairing code, or it has expired"}, "expired"),
    (409, {"detail": "already claimed by another machine"}, "someone else"),
    (429, {"detail": "too many pairing requests"}, "too many"),
    (503, None, "answered 503"),
])
def test_each_way_the_claim_can_fail_is_its_own_sentence(
        agent_mod, tmp_path, monkeypatch, capsys, status, body, phrase):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    http, _ = _server({"/pair/claim": [(status, body)]})
    monkeypatch.setattr(agent_mod, "_http_json", http)
    assert agent_mod.main(["--pair", "K7QX-4MRP", "--server", "https://x"]) == 2
    assert phrase in capsys.readouterr().out
    assert not (tmp_path / "jarvis" / "computeruse.json").exists()


def test_a_denial_and_an_expiry_while_waiting_stop_cleanly(
        agent_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    for reply, phrase in (((200, {"ok": True, "state": "denied"}), "denied"),
                          ((404, {"detail": "no such pairing code"}), "expired")):
        http, _ = _server({
            "/pair/claim": [(200, {"ok": True, "device_secret": DEVICE})],
            "/pair/poll": [(200, {"ok": True, "state": "claimed"}), reply],
        })
        monkeypatch.setattr(agent_mod, "_http_json", http)
        assert agent_mod.main(["--pair", "K7QX-4MRP", "--server", "https://x"]) == 2
        assert phrase in capsys.readouterr().out
    assert not (tmp_path / "jarvis" / "computeruse.json").exists()


def test_pair_needs_a_server(agent_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(SystemExit):
        agent_mod.main(["--pair", "K7QX-4MRP"])


def test_redirects_are_reported_not_followed(agent_mod, monkeypatch):
    """Access answers a bare request with a 302 to its login page. urllib
    follows that by default and hands back a 200 full of HTML, which turns
    'this path needs a Bypass policy' into 'not valid JSON'."""
    import urllib.error
    import urllib.request

    class Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                         {"Location": "https://sso.example/"}, None)

    seen = {}

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    status, body, raw = agent_mod._http_json("https://x/api/computeruse/pair/claim",
                                             {"code": "K7QX-4MRP"})
    assert status == 302 and body is None
    # the handler that refuses redirects is installed
    assert seen["handlers"] and seen["handlers"][0].redirect_request(None) is None
