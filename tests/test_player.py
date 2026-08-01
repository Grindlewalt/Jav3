"""The in-page music player: the stream proxy, the report channel, the routing.

The player exists because TARMAC is a SEPARATE Cloudflare Access application
from Jarvis, so the browser's Jarvis session cannot fetch its /stream/:id. The
host proxies instead. Two things in here are load-bearing and easy to break
without noticing:

  * Range must survive both directions. Without Content-Range an <audio> element
    cannot seek and Safari will not start at all — and a proxy that quietly
    returns 200-with-everything looks fine in a smoke test.
  * "accepted" and "audible" are different facts. A browser refuses play() in a
    tab with no user gesture, so the tool must report what the tab actually did
    rather than that the request was taken.
"""
import asyncio

import httpx
import pytest

from backend import computeruse_api, gui, tarmac


@pytest.fixture
def configured(monkeypatch):
    async def cfg():
        return "https://music.example", "id.access", "secret"
    monkeypatch.setattr(tarmac, "get_config", cfg)


def _mock(handler, monkeypatch):
    """Point tarmac's httpx clients at a stub instead of the network."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Client)


# the in-page player state is reset between tests by an autouse fixture in
# conftest.py — it is a process global and would otherwise leak across files


# --- the stream proxy ---------------------------------------------------------

@pytest.mark.asyncio
async def test_range_is_forwarded_and_the_206_passed_back(configured, monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["range"] = request.headers.get("range")
        # the Access headers are ours to attach, on the audio path too
        seen["cf"] = request.headers.get("CF-Access-Client-Id")
        return httpx.Response(206, content=b"PARTIAL", headers={
            "content-type": "audio/mpeg", "content-range": "bytes 10-16/999",
            "accept-ranges": "bytes", "content-length": "7",
            "set-cookie": "leak=1"})

    _mock(handler, monkeypatch)
    handle = await tarmac.open_stream(42, "bytes=10-16")

    assert seen["path"] == "/stream/42"
    assert seen["range"] == "bytes=10-16"
    assert seen["cf"] == "id.access"
    assert handle.status == 206
    assert handle.headers["content-range"] == "bytes 10-16/999"
    assert handle.headers["accept-ranges"] == "bytes"
    # only the headers on the allowlist come back — a Set-Cookie from the music
    # host has no business being replayed onto Jarvis's origin
    assert "set-cookie" not in {k.lower() for k in handle.headers}

    assert b"".join([c async for c in handle.chunks()]) == b"PARTIAL"


@pytest.mark.asyncio
async def test_a_plain_fetch_streams_the_whole_body(configured, monkeypatch):
    _mock(lambda r: httpx.Response(200, content=b"AUDIO",
                                   headers={"content-type": "audio/mpeg"}),
          monkeypatch)
    handle = await tarmac.open_stream(7)
    assert handle.status == 200
    assert b"".join([c async for c in handle.chunks()]) == b"AUDIO"


@pytest.mark.asyncio
async def test_consuming_the_stream_closes_the_client(configured, monkeypatch):
    """The handle owns an httpx client per listener. If chunks() did not close
    it, every track played would leak a connection pool."""
    _mock(lambda r: httpx.Response(200, content=b"x"), monkeypatch)
    handle = await tarmac.open_stream(1)
    async for _ in handle.chunks():
        pass
    assert handle._client.is_closed


@pytest.mark.asyncio
async def test_a_cloudflare_redirect_on_audio_names_the_separate_application(
        configured, monkeypatch):
    _mock(lambda r: httpx.Response(302, headers={
        "location": "https://x.cloudflareaccess.com/cdn-cgi/access/login"}),
        monkeypatch)
    with pytest.raises(tarmac.TarmacError) as e:
        await tarmac.open_stream(42)
    assert "SEPARATE Access application" in str(e.value)


@pytest.mark.asyncio
async def test_a_missing_track_on_the_stream_path_is_plain(configured, monkeypatch):
    _mock(lambda r: httpx.Response(404), monkeypatch)
    with pytest.raises(tarmac.TarmacError) as e:
        await tarmac.open_stream(999)
    assert "does not exist" in str(e.value)


@pytest.mark.asyncio
async def test_the_route_passes_status_and_headers_through(monkeypatch):
    """The route must not flatten a 206 into a 200 — that is exactly the bug
    that breaks seeking while still sounding like it works."""
    class Fake:
        status = 206
        headers = {"content-range": "bytes 0-3/9", "content-type": "audio/mpeg"}

        async def chunks(self):
            yield b"ABCD"

    async def fake_open(track_id, rng):
        assert (track_id, rng) == (5, "bytes=0-3")
        return Fake()

    monkeypatch.setattr(tarmac, "open_stream", fake_open)
    req = httpx.Request("GET", "http://t/x", headers={"range": "bytes=0-3"})
    resp = await computeruse_api.tarmac_stream(5, req)

    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-3/9"


@pytest.mark.asyncio
async def test_an_unreachable_music_server_is_a_502_not_a_traceback(monkeypatch):
    from fastapi import HTTPException

    async def boom(track_id, rng):
        raise tarmac.TarmacError("could not reach the music server: nope")

    monkeypatch.setattr(tarmac, "open_stream", boom)
    with pytest.raises(HTTPException) as e:
        await computeruse_api.tarmac_stream(1, httpx.Request("GET", "http://t/x"))
    assert e.value.status_code == 502


# --- shuffle: an action we were refusing that the server always accepted ------

@pytest.mark.asyncio
async def test_shuffle_reaches_the_server(configured, monkeypatch):
    sent = {}

    def handler(request):
        sent.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={"ok": True, "players": 1, "queued": []})

    _mock(handler, monkeypatch)
    await tarmac.remote("shuffle", n=5, tag="drive")
    assert sent == {"action": "shuffle", "tag": "drive", "n": 5}


@pytest.mark.asyncio
async def test_shuffle_still_validates_the_tag(configured, monkeypatch):
    _mock(lambda r: httpx.Response(200, json={"ok": True}), monkeypatch)
    with pytest.raises(tarmac.TarmacError):
        await tarmac.remote("shuffle", tag="jazz")


# --- the report channel -------------------------------------------------------

def test_a_silent_tab_goes_stale_rather_than_lying():
    """A closed laptop leaves its last report sitting in memory forever. Without
    the staleness check the router would keep sending music to a tab that is
    not there."""
    gui.player_report({"track": {"id": 1, "title": "x"}, "paused": False,
                       "started": True})
    assert gui.player_status()["stale"] is False
    gui._player["reported_at"] -= 120
    assert gui.player_status()["stale"] is True


def test_a_new_play_clears_the_previous_verdict(monkeypatch):
    """started/error describe one track. Carrying them into the next play would
    report the last song's silence as this song's success."""
    gui.player_report({"track": {"id": 1}, "started": True, "error": "boom"})
    monkeypatch.setattr(gui, "push", lambda ev, tab=None: 1)
    gui.player_push("play", queue=[], index=0)
    s = gui.player_status()
    assert s["started"] is False and s["error"] == ""


def test_the_stream_url_is_same_origin():
    assert gui.stream_url(42) == "/api/computeruse/tarmac/stream/42"


# --- destination routing ------------------------------------------------------

def test_auto_prefers_the_player_that_can_actually_make_sound(monkeypatch):
    from tools.music_play.handler import _resolve_where

    monkeypatch.setattr(gui, "tabs", lambda: 1)
    assert _resolve_where("auto") == "jarvis"
    monkeypatch.setattr(gui, "tabs", lambda: 0)
    assert _resolve_where("auto") == "app"
    # an explicit choice always wins over the guess
    monkeypatch.setattr(gui, "tabs", lambda: 0)
    assert _resolve_where("jarvis") == "jarvis"
    monkeypatch.setattr(gui, "tabs", lambda: 5)
    assert _resolve_where("app") == "app"


@pytest.mark.asyncio
async def test_playing_in_page_with_no_tab_open_says_so(configured, monkeypatch):
    from tools.music_play.handler import run

    monkeypatch.setattr(gui, "tabs", lambda: 0)
    monkeypatch.setattr(gui, "player_push", lambda a, **k: 0)
    _mock(lambda r: httpx.Response(200, json={
        "id": 3, "title": "Nightcall", "artist": "Kavinsky", "duration": 258}),
        monkeypatch)

    out = await run(ids=[3], where="jarvis")
    assert "no Jarvis tab is open" in out
    assert "where='app'" in out


@pytest.mark.asyncio
async def test_a_browser_refusal_is_reported_not_swallowed(configured, monkeypatch):
    """The whole point of the report channel: the tab took the track and the
    browser still refused to make noise."""
    from tools.music_play import handler

    # handler.asyncio IS the asyncio module, so the real sleep has to be
    # captured before it is replaced or the stub calls itself
    real_sleep = asyncio.sleep
    monkeypatch.setattr(handler.asyncio, "sleep", lambda *_: real_sleep(0))
    monkeypatch.setattr(gui, "player_push", lambda a, **k: 1)
    # music now plays in ONE tab, so there has to be one open. With none, the
    # honest answer is "nowhere to play" — see tests/test_gui_tabs.py.
    monkeypatch.setattr(gui, "resolve_tab", lambda want, asked: ("t1", "Mac"))
    monkeypatch.setattr(gui, "player_status", lambda: {
        "track": {"id": 3}, "started": False, "paused": True,
        "error": "the browser blocked autoplay until the page is clicked"})
    _mock(lambda r: httpx.Response(200, json={
        "id": 3, "title": "Nightcall", "artist": "Kavinsky", "duration": 258}),
        monkeypatch)

    out = await handler.run(ids=[3], where="jarvis")
    assert "refused to start it" in out
    assert "blocked autoplay" in out


@pytest.mark.asyncio
async def test_a_confirmed_in_page_play_says_where_it_went(configured, monkeypatch):
    from tools.music_play import handler

    # handler.asyncio IS the asyncio module, so the real sleep has to be
    # captured before it is replaced or the stub calls itself
    real_sleep = asyncio.sleep
    monkeypatch.setattr(handler.asyncio, "sleep", lambda *_: real_sleep(0))
    monkeypatch.setattr(gui, "player_push", lambda a, **k: 1)
    # music now plays in ONE tab, so there has to be one open. With none, the
    # honest answer is "nowhere to play" — see tests/test_gui_tabs.py.
    monkeypatch.setattr(gui, "resolve_tab", lambda want, asked: ("t1", "Mac"))
    monkeypatch.setattr(gui, "player_status", lambda: {
        "track": {"id": 3}, "started": True, "paused": False, "error": ""})
    _mock(lambda r: httpx.Response(200, json={
        "id": 3, "title": "Nightcall", "artist": "Kavinsky", "duration": 258}),
        monkeypatch)

    out = await handler.run(ids=[3], where="jarvis")
    assert "in the Jarvis player" in out


@pytest.mark.asyncio
async def test_the_queue_carries_stream_urls_and_durations(configured, monkeypatch):
    """The scrubber needs a duration, and the <audio> element needs a
    same-origin src — a caller working from bare ids has neither."""
    from tools.music_play.handler import _queue_rows

    _mock(lambda r: httpx.Response(200, json={
        "id": int(r.url.path.rsplit("/", 1)[-1]), "title": "T", "artist": "A",
        "album": "L", "duration": 200}), monkeypatch)

    rows = await _queue_rows([3, 4])
    assert [r["src"] for r in rows] == [
        "/api/computeruse/tarmac/stream/3", "/api/computeruse/tarmac/stream/4"]
    assert rows[0]["duration"] == 200


# --- transport ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_volume_on_the_music_app_refuses_instead_of_pretending():
    """TARMAC's remote API has no volume. Silently accepting the call and doing
    nothing is the failure mode worth designing out."""
    from tools.music_control.handler import run

    out = await run(action="volume", level=40, where="app")
    assert "no volume control" in out
    assert "where='jarvis'" in out


@pytest.mark.asyncio
async def test_volume_on_the_in_page_player_works(monkeypatch):
    from tools.music_control.handler import run

    pushed = {}

    def fake_push(action, tab=None, **fields):
        pushed.update({"action": action, "tab": tab, **fields})
        return 1

    monkeypatch.setattr(gui, "player_push", fake_push)
    monkeypatch.setattr(gui, "resolve_tab", lambda want, asked: ("t1", "Mac"))
    out = await run(action="volume", level=250, where="jarvis")
    # tab is the addressing, not a field of the action
    assert pushed == {"action": "volume", "tab": "t1", "level": 100}
    assert "100%" in out


@pytest.mark.asyncio
async def test_transport_auto_follows_whichever_player_holds_the_track(monkeypatch):
    from tools.music_control.handler import _resolve_where

    gui.player_report({"track": {"id": 9}, "paused": False, "started": True})
    assert _resolve_where("auto") == "jarvis"
    gui.player_report({"track": None})
    assert _resolve_where("auto") == "app"


@pytest.mark.asyncio
async def test_status_reports_both_players_separately(configured, monkeypatch):
    """They cannot see each other, so merging them would be a guess."""
    from tools.music_status.handler import run

    monkeypatch.setattr(gui, "tabs", lambda: 1)
    gui.player_report({"track": {"id": 1, "title": "Nightcall",
                                 "artist": "Kavinsky"},
                       "paused": False, "started": True, "position": 61.5,
                       "duration": 258, "volume": 80, "queue": 2, "error": ""})
    _mock(lambda r: httpx.Response(200, json={
        "ok": True, "tracks": 412, "players_connected": 0, "now_playing": None}),
        monkeypatch)

    out = await run()
    assert "Jarvis player: playing Nightcall — Kavinsky" in out
    assert "1:01 of 4:18" in out
    assert "2 more queued" in out
    assert "music app players open: 0" in out
    assert "412 tracks" in out
