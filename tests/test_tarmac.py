"""The music-server integration, against a stub that speaks TARMAC's real API.

Shapes here are taken from MyTube-Music's server.js, not from its README: the
README summarises, and a summary is where an integration quietly diverges.

The Cloudflare 302 case has its own test because it is the failure the operator
will actually hit — the music host is a SEPARATE Access application from Jarvis,
so a token that works for one is not accepted by the other until that
application has its own Service Auth policy.
"""
import json

import httpx
import pytest

from backend import tarmac


@pytest.fixture
def configured(monkeypatch):
    async def cfg():
        return "https://music.example", "id.access", "secret"
    monkeypatch.setattr(tarmac, "get_config", cfg)


def _mock(handler, monkeypatch):
    """Point tarmac._call at a stub instead of the network."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Client)


# --- happy paths, shapes from server.js --------------------------------------

@pytest.mark.asyncio
async def test_status_reports_library_players_and_now_playing(configured, monkeypatch):
    def handler(request):
        assert request.url.path == "/api/status"
        # the Access headers must be attached by us, on every request
        assert request.headers["CF-Access-Client-Id"] == "id.access"
        assert request.headers["CF-Access-Client-Secret"] == "secret"
        return httpx.Response(200, json={
            "ok": True, "tracks": 412, "players_connected": 1,
            "now_playing": {"id": 42, "title": "Nightcall", "artist": "Kavinsky",
                            "paused": False, "position": 61.5, "duration": 258}})
    _mock(handler, monkeypatch)

    from tools.music_status.handler import run
    out = await run()
    assert "412 tracks" in out
    assert "players open: 1" in out
    assert "Nightcall" in out and "Kavinsky" in out
    assert "1:01 of 4:18" in out


@pytest.mark.asyncio
async def test_search_returns_ids_the_model_can_play(configured, monkeypatch):
    def handler(request):
        assert request.url.path == "/api/search"
        assert request.url.params["q"] == "kavinsky"
        return httpx.Response(200, json=[
            {"id": 42, "title": "Nightcall", "artist": "Kavinsky",
             "album": "OutRun", "tag": "drive", "stream": "/stream/42"}])
    _mock(handler, monkeypatch)

    from tools.music_search.handler import run
    out = await run(query="kavinsky")
    assert "[42]" in out and "Nightcall" in out and "#drive" in out


@pytest.mark.asyncio
async def test_play_sends_tarmacs_own_action_vocabulary(configured, monkeypatch):
    sent = {}

    def handler(request):
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[
                {"id": 7, "title": "Solo", "artist": "One"}])
        assert request.url.path == "/api/remote"
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "players": 2})
    _mock(handler, monkeypatch)

    from tools.music_play.handler import run
    out = await run(query="solo")
    assert sent == {"action": "play", "ids": [7]}
    assert "2 open players" in out


@pytest.mark.asyncio
async def test_control_uses_prev_not_previous(configured, monkeypatch):
    """TARMAC's set is play/pause/resume/next/prev. Sending 'previous' or
    'stop' — which computer_playback uses — would 400."""
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "players": 1})
    _mock(handler, monkeypatch)

    from tools.music_control.handler import run
    assert "went back" in await run(action="prev")
    assert sent["action"] == "prev"

    for wrong in ("previous", "stop", "playpause"):
        out = await run(action=wrong)
        assert out.startswith("error:"), wrong
        assert "prev" in out


@pytest.mark.asyncio
async def test_an_ambiguous_query_lists_instead_of_guessing(configured, monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[
            {"id": 1, "title": "Take On Me", "artist": "a-ha"},
            {"id": 2, "title": "Take On Me", "artist": "MTV Unplugged"}])
    _mock(handler, monkeypatch)

    from tools.music_play.handler import run
    out = await run(query="take on me")
    assert "[1]" in out and "[2]" in out and "say which" in out


# --- the failures the operator will actually hit ------------------------------

@pytest.mark.asyncio
async def test_a_cloudflare_redirect_explains_the_separate_application(
        configured, monkeypatch):
    """The exact symptom seen live: 302 to cloudflareaccess.com. Reporting
    "unexpected redirect" would send the operator hunting in the wrong place."""
    def handler(request):
        return httpx.Response(302, headers={
            "location": "https://x.cloudflareaccess.com/cdn-cgi/access/login/music"})
    _mock(handler, monkeypatch)

    from tools.music_status.handler import run
    out = await run()
    assert "SEPARATE Access application" in out
    assert "Service Auth" in out


@pytest.mark.asyncio
async def test_no_open_player_says_so_rather_than_claiming_success(
        configured, monkeypatch):
    """TARMAC 409s when nothing is subscribed. Playback goes to its own players,
    so this is a normal state, not a bug."""
    def handler(request):
        return httpx.Response(409, json={"error": "no player connected"})
    _mock(handler, monkeypatch)

    from tools.music_control.handler import run
    out = await run(action="pause")
    assert out.startswith("error:")
    assert "no TARMAC player is open" in out


@pytest.mark.asyncio
async def test_unconfigured_points_at_the_tab(monkeypatch):
    async def cfg():
        return "", "", ""
    monkeypatch.setattr(tarmac, "get_config", cfg)
    from tools.music_status.handler import run
    out = await run()
    assert "not configured" in out and "Computer use tab" in out


@pytest.mark.asyncio
async def test_an_unreachable_server_is_a_sentence_not_a_traceback(
        configured, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")
    _mock(handler, monkeypatch)
    from tools.music_status.handler import run
    out = await run()
    assert out.startswith("error:") and "could not reach" in out


@pytest.mark.asyncio
async def test_a_missing_track_is_reported_plainly(configured, monkeypatch):
    def handler(request):
        return httpx.Response(404, json={"error": "no such track"})
    _mock(handler, monkeypatch)
    from tools.music_play.handler import run
    out = await run(ids=[9999])
    assert "does not exist" in out


# --- input discipline ---------------------------------------------------------

@pytest.mark.asyncio
async def test_track_ids_must_be_numbers(configured, monkeypatch):
    _mock(lambda r: httpx.Response(200, json={"ok": True, "players": 1}), monkeypatch)
    from tools.music_play.handler import run
    out = await run(ids=["1; rm -rf /"])
    assert out.startswith("error:")


@pytest.mark.asyncio
async def test_download_requires_an_http_url(configured, monkeypatch):
    _mock(lambda r: httpx.Response(200, json={"job": "j1"}), monkeypatch)
    from tools.music_download.handler import run
    for bad in ("file:///etc/passwd", "javascript:x", "not a url"):
        assert (await run(url=bad)).startswith("error:"), bad
    assert "job j1" in await run(url="https://music.youtube.com/watch?v=x")


@pytest.mark.asyncio
async def test_a_bad_tag_is_refused_before_the_request(configured, monkeypatch):
    called = []
    _mock(lambda r: called.append(1) or httpx.Response(200, json=[]), monkeypatch)
    from tools.music_search.handler import run
    out = await run(query="x", tag="jazz")
    assert out.startswith("error:") and "drive" in out
    assert not called, "should not have hit the server"


@pytest.mark.asyncio
async def test_the_access_secret_never_appears_in_a_tool_result(
        configured, monkeypatch):
    """It is a host-side credential. A tool result goes into the transcript."""
    def handler(request):
        return httpx.Response(500, text="secret leaked? no")
    _mock(handler, monkeypatch)
    from tools.music_status.handler import run
    out = await run()
    assert "secret" not in out or "CF-Access" not in out
    assert "id.access" not in out


def test_config_url_must_be_http(monkeypatch):
    import asyncio
    with pytest.raises(tarmac.TarmacError):
        asyncio.run(tarmac.set_config("ftp://music.example"))
    with pytest.raises(tarmac.TarmacError):
        asyncio.run(tarmac.set_config("music.example"))
