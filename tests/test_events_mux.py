"""One connection for every live feed: GET /api/events.

The operator's report: with two Jav3 tabs open over plain http, the Work page's
"+ window" never opened. Each tab held three or four SSE feeds for its whole
life, a browser allows six connections per host across ALL tabs, and the
layout GET queued forever behind them. These tests pin the multiplexer that
replaces those feeds with one: topics filtered, events tagged, authorisation
per topic, a GUI event for one tab still marked for that tab, and every
subscription released when the client goes.
"""
import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from backend import agents_run, bus, egress, events_api, gui, security, sse
from backend.auth import COOKIE_NAME, make_token
from backend.egress_api import channel_feed
from backend.main import app


def _request(cookie: str | None) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/events",
                    "headers": headers, "query_string": b""})


async def _next(it, timeout=2.0):
    """The next non-keepalive frame, parsed."""
    while True:
        chunk = await asyncio.wait_for(it.__anext__(), timeout)
        if chunk.startswith(":"):
            continue
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        return json.loads(chunk[6:])


@pytest.fixture
def token(tmp_env):
    return make_token(1, "operator")


async def _open(token, topics):
    resp = await events_api.events(_request(token), topics=topics)
    return resp.body_iterator


async def test_topics_are_filtered_and_tagged(token):
    it = await _open(token, "security,notices")
    try:
        assert await _next(it) == {"topic": "security", "event": {
            "type": "stream_open", "channel": security.SECURITY_CHAN}}
        assert await _next(it) == {"topic": "notices",
                                   "event": {"type": "stream_open"}}
        bus.publish(egress.EGRESS_CHAN, {"type": "egress", "host": "x"})  # not asked for
        bus.publish(security.SECURITY_CHAN, {"type": "security_event", "id": 1})
        bus.publish(agents_run.NOTICE_CHAN, {"type": "agent_run_done", "n": 1})
        bus.publish(security.SECURITY_CHAN, {"type": "security_event", "id": 2})
        got = [await _next(it) for _ in range(3)]
        assert {"topic": "notices", "event": {"type": "agent_run_done", "n": 1}} in got
        sec = [g["event"]["id"] for g in got if g["topic"] == "security"]
        assert sec == [1, 2], "order within a topic is kept"
        assert all(g["topic"] != "egress" for g in got)
    finally:
        await it.aclose()


async def test_default_is_every_topic(token):
    it = await _open(token, "")
    try:
        seen = {(await _next(it))["topic"] for _ in range(len(events_api.TOPICS))}
        assert seen == set(events_api.TOPICS)
    finally:
        await it.aclose()


async def test_disconnect_releases_every_subscription(token):
    before = {c: bus.subscriber_count(c) for c in
              (gui.GUI_CHAN, security.SECURITY_CHAN, egress.EGRESS_CHAN,
               agents_run.NOTICE_CHAN)}
    it = await _open(token, "gui,security,notices,egress")
    await _next(it)
    for c, n in before.items():
        assert bus.subscriber_count(c) == n + 1
    await it.aclose()
    for c, n in before.items():
        assert bus.subscriber_count(c) == n, c
    assert gui._mux == {}


async def test_cancel_releases_every_subscription(token):
    """A browser going away cancels the response task mid-await, not aclose."""
    it = await _open(token, "security,egress")
    await _next(it)
    await _next(it)
    task = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await task
    assert bus.subscriber_count(security.SECURITY_CHAN) == 0
    assert bus.subscriber_count(egress.EGRESS_CHAN) == 0


async def test_one_keepalive_for_the_whole_stream(token, monkeypatch):
    monkeypatch.setattr(sse, "KEEPALIVE_S", 0.05)
    it = await _open(token, "security,notices")
    try:
        await _next(it)
        await _next(it)
        chunk = await asyncio.wait_for(it.__anext__(), 1)
        assert chunk == ": keepalive\n\n"
    finally:
        await it.aclose()


async def test_a_gui_push_for_one_tab_names_that_tab(token):
    """Several tabs share the leader's connection; a push for one of them must
    still say which, or music plays on every machine again."""
    it = await _open(token, "gui")
    try:
        opened = await _next(it)
        assert opened["topic"] == "gui" and opened["event"]["type"] == "stream_open"
        conn = opened["event"]["conn"]
        assert gui.set_conn_tabs(conn, [{"id": "t-a", "name": "Mac · Chrome"},
                                        {"id": "t-b", "name": "Mac · Chrome 2"}]) == 2
        assert gui.tabs() == 2, "one connection, two tabs behind it"
        assert gui.resolve_tab("chrome 2", None)[0] == "t-b"

        assert gui.push({"type": "player", "action": "play"}, tab="t-b") == 1
        assert await _next(it) == {"topic": "gui", "to": "t-b",
                                   "event": {"type": "player", "action": "play"}}
        assert gui.push({"type": "layout_changed", "slug": "x"}) == 2
        ev = await _next(it)
        assert "to" not in ev and ev["event"]["type"] == "layout_changed"

        gui.set_conn_tabs(conn, [{"id": "t-a", "name": "Mac · Chrome"}])
        assert gui.push({"type": "player"}, tab="t-b") == 0, "a closed tab is gone"
    finally:
        await it.aclose()
    assert gui.tab_list() == [] and gui.tabs() == 0


async def test_a_tab_moving_to_a_new_leader_survives_the_old_ones_cleanup(token):
    old = await _open(token, "gui")
    new = await _open(token, "gui")
    try:
        c_old = (await _next(old))["event"]["conn"]
        c_new = (await _next(new))["event"]["conn"]
        gui.set_conn_tabs(c_old, [{"id": "t-a", "name": "a"}])
        gui.set_conn_tabs(c_new, [{"id": "t-a", "name": "a"}])
        await old.aclose()
        assert [t["id"] for t in gui.tab_list()] == ["t-a"]
        gui.push({"type": "player"}, tab="t-a")
        assert (await _next(new))["to"] == "t-a"
    finally:
        await new.aclose()


def test_unknown_conn_is_none():
    assert gui.set_conn_tabs("nope", [{"id": "x"}]) is None


async def test_refused_topic_refuses_the_request_and_subscribes_nothing(token, monkeypatch):
    from fastapi import HTTPException

    def admin_only(request):
        raise HTTPException(status_code=403, detail="not for you")

    monkeypatch.setitem(events_api.TOPICS, "egress",
                        (admin_only, events_api.TOPICS["egress"][1]))
    with pytest.raises(HTTPException) as e:
        await events_api.events(_request(token), topics="security,egress")
    assert e.value.status_code == 403 and "egress" in e.value.detail
    assert bus.subscriber_count(security.SECURITY_CHAN) == 0
    it = await _open(token, "security")            # the others still readable
    assert (await _next(it))["topic"] == "security"
    await it.aclose()


async def test_http_auth_and_unknown_topics(tmp_env):
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://test") as c:
        assert (await c.get("/api/events")).status_code == 401
        c.cookies.set(COOKIE_NAME, make_token(1, "operator"))
        r = await c.get("/api/events?topics=security,bogus")
        assert r.status_code == 400 and "bogus" in r.text
        r = await c.put("/api/gui/conn/nope/tabs", json={"tabs": []},
                        headers={"Origin": "http://test"})
        assert r.status_code == 404


async def test_the_old_per_feed_endpoints_share_the_code():
    resp = sse.sse_response(channel_feed(security.SECURITY_CHAN))
    it = resp.body_iterator
    assert await _next(it) == {"type": "stream_open",
                               "channel": security.SECURITY_CHAN}
    bus.publish(security.SECURITY_CHAN, {"type": "security_event", "id": 9})
    assert (await _next(it))["id"] == 9
    await it.aclose()
    assert bus.subscriber_count(security.SECURITY_CHAN) == 0

    resp = sse.sse_response(gui.tab_subscription("t-x", "X"))
    it = resp.body_iterator
    assert await _next(it) == {"type": "stream_open", "channel": gui.GUI_CHAN,
                               "tab": "t-x"}
    assert gui.push({"type": "player"}, tab="t-x") == 1
    assert await _next(it) == {"type": "player"}, "no tab stamp on a tab's own stream"
    await it.aclose()
    assert gui.tab_list() == []
