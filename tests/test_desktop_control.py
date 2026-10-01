"""The operator takes a box's desktop, then hands it back (live desktop P3).

Control is a field on the box's desktop (backend/vm/boxdesk.py): `agent` or
`operator`. While the operator holds it three locks keep the agent's hands off:
desk.act refuses input verbs (screenshots stay allowed), the guest seat is told
input is off, and the RFB filter admits key and pointer only from the holder's
own socket. Offline: the "guest" is the REAL jav3-desk Session on a fake X11
backend (as in test_desk_box) and a fake RFB listener (as in test_desktop_display).
"""
import asyncio
import json
import socket
import time

import pytest

from backend import bus, desk, runtime
from backend.db import get_db, init_db
from backend.vm import boxdesk, boxes, display_api
import test_desk_box as tdb
import test_desktop_display as tdd
from test_desk_box import _rows, _tool, turn
from test_desktop_display import (FBU, HANDSHAKE, QEMU_KEY, RESIZE, FakeGuest, _desktop_box,
                                  cut_text, key, pointer)

# the fixtures of those two files, assigned (not imported by name) so pytest finds
# them here: the seat's fake X11 guest + a desktop box, and the TestClient host
world, _fast, host = tdb.world, tdb._fast, tdd.host

VIEWER = "viewer-aaaa1111"
OTHER = "viewer-bbbb2222"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setattr(desk, "SHOTS_PER_S", 1000)         # these tests look at the screen often


class Recorder:
    """Stands in for a viewer socket's key release, and records the seat's frames."""

    def __init__(self):
        self.released = 0

    async def __call__(self):
        self.released += 1


def _record_seat(d):
    """Every grants frame the host sends the seat, decoded."""
    sent = []
    orig = d.ws.send_text

    async def rec(text):
        if json.loads(text).get("type") == "grants":
            sent.append(json.loads(text))
        await orig(text)
    d.ws.send_text = rec
    return sent


async def _events(kind):
    db = await get_db()
    try:
        async with db.execute("SELECT kind, severity, summary, detail, acknowledged, actor, quiet "
                              "FROM security_events WHERE kind = ? ORDER BY id", (kind,)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _grants_row(did):
    rows = await _rows("SELECT screen, input, shell FROM desk_grants WHERE device_id = ?", did)
    return rows[0]


# --- the three locks ------------------------------------------------------------------

async def test_taking_control_pauses_the_agent_and_handing_back_restores_it(world):
    box, fx = world["box"], world["fx"]
    d = await boxdesk.ensure(box)
    seat = _record_seat(d)
    rel = Recorder()
    boxdesk.viewer_up(box.id, VIEWER, rel)
    with turn("op-a", box, "game"):
        shot = await _tool("desk_screenshot")(computer="sandbox")
        assert not shot.startswith("error")
        st = await boxdesk.take(box, VIEWER, "grant")
        assert st["holder"] == "operator" and st["viewer"] == VIEWER and st["by"] == "grant"
        assert boxdesk.control_state(box.id)["holder"] == "operator"

        # lock 1: act() refuses every input verb with the sentence the model can act on
        for tool, kw in (("desk_click", {"x": 5, "y": 5}), ("desk_type", {"text": "hi"}),
                         ("desk_key", {"combo": "Return"})):
            out = await _tool(tool)(computer="sandbox", **kw)
            assert out == ("error: the operator has taken control of the sandbox desktop; stop "
                           "and wait. Screenshots still work; input comes back when they hand "
                           "it back"), out
        assert not fx.calls                                    # nothing reached the box
        # ... screenshots stay allowed, so the agent can watch
        assert not (await _tool("desk_screenshot")(computer="sandbox")).startswith("error")

        # lock 2: the seat was told input is off, and the guest refuses on its own
        assert seat[-1] == {"type": "grants", "screen": True, "input": False, "shell": "off"}
        res = await desk._call(d, "click", {"x": 5, "y": 5}, 5)       # bypassing act()
        assert res.get("ok") is False and "input is not granted" in str(res.get("err"))
        assert not fx.calls
        # ... and the grants row was NOT rewritten: a host restart cannot lock the agent out
        assert (await _grants_row(d.device_id))["input"] == 1

        # the operator turns input back on in Settings meanwhile: the seat still hears off
        await desk.set_grants(d.device_id, input=True)
        assert seat[-1]["input"] is False

        # hand back: the real grants go to the seat, the frame is forgotten
        st = await boxdesk.hand_back(box.id)
        assert st["holder"] == "agent" and rel.released == 1
        assert seat[-1] == {"type": "grants", "screen": True, "input": True, "shell": "off"}
        assert d.frame is None and d.operator_since is None
        # the agent's next result says so (an error keeps its first line)
        first = await _tool("desk_click")(x=5, y=5, computer="sandbox")
        assert first.startswith("error:") and "desk_screenshot" in first        # frame was reset
        assert "the operator used this desktop for " in first and " s" in first.split("\n")[1]
        # ... once
        shot = await _tool("desk_screenshot")(computer="sandbox")
        assert "the operator used" not in shot
        clicked = await _tool("desk_click")(x=5, y=5, computer="sandbox")
        assert not clicked.startswith("error"), clicked
        assert ("click", "left", 1) in fx.calls                  # the box got it, once it was handed back


async def test_the_note_prefixes_a_successful_result_and_sums_the_take_overs(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    with turn("op-a", box, "game"):
        await boxdesk.take(box, VIEWER, "grant")
        await boxdesk.hand_back(box.id)
        await boxdesk.take(box, VIEWER, "grant")
        await boxdesk.hand_back(box.id)
        assert d.used_by_operator_s == 2                # each hand back counts at least a second
        out = await _tool("desk_screenshot")(computer="sandbox")
        assert out.startswith("the operator used this desktop for 2 s")
        assert "sandbox: screenshot 1280x800 attached" in out


async def test_a_seat_that_registers_while_the_operator_holds_it_starts_paused(world):
    box, fx = world["box"], world["fx"]
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    await boxdesk.take(box, VIEWER, "grant")             # no seat yet
    d = await boxdesk.ensure(box)
    assert d.operator_since is not None
    with turn("op-a", box, "game"):
        assert "taken control" in await _tool("desk_click")(x=1, y=1, computer="sandbox")
    res = await desk._call(d, "click", {"x": 5, "y": 5}, 5)
    assert res.get("ok") is False and not fx.calls


async def test_a_stop_in_settings_during_a_take_over_is_not_undone_by_the_hand_back(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    await boxdesk.take(box, VIEWER, "grant")
    await desk.stop(d.device_id, by="grant")
    for _ in range(100):
        if boxdesk.live(box.id) is None:
            break
        await asyncio.sleep(0.02)
    await boxdesk.hand_back(box.id)
    g = await _grants_row(d.device_id)
    assert (g["screen"], g["input"], g["shell"]) == (0, 0, "off")     # Stop stays a Stop


async def test_only_one_window_holds_control_and_it_must_be_connected(world):
    box = world["box"]
    await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    boxdesk.viewer_up(box.id, OTHER, Recorder())
    with pytest.raises(boxdesk.ControlError, match="not connected"):
        await boxdesk.take(box, "never-connected", "grant")
    with pytest.raises(boxdesk.ControlError, match="not connected"):
        await boxdesk.take(box, "", "grant")
    await boxdesk.take(box, VIEWER, "grant")
    assert (await boxdesk.take(box, VIEWER, "grant"))["viewer"] == VIEWER     # idempotent
    with pytest.raises(boxdesk.ControlError, match="another window"):
        await boxdesk.take(box, OTHER, "grant")
    assert boxdesk.control_state(box.id)["viewer"] == VIEWER
    assert (await boxdesk.hand_back(box.id))["holder"] == "agent"
    assert (await boxdesk.hand_back(box.id))["holder"] == "agent"             # a no-op


# --- the holder's window dropping: a grace, then the agent has it back ---------------------

async def test_the_holders_window_dropping_hands_back_after_the_grace(world, monkeypatch):
    monkeypatch.setattr(boxdesk, "GRACE_S", 0.15)
    box = world["box"]
    d = await boxdesk.ensure(box)
    rel = Recorder()
    boxdesk.viewer_up(box.id, VIEWER, rel)
    await boxdesk.take(box, VIEWER, "grant")
    boxdesk.viewer_down(box.id, VIEWER, rel)
    assert boxdesk.control_state(box.id)["holder"] == "operator"      # not yet: the grace runs
    await asyncio.sleep(0.5)
    assert boxdesk.control_state(box.id)["holder"] == "agent"
    assert d.operator_since is None and d.used_by_operator_s >= 1
    rows = await _rows("SELECT params FROM desk_actions WHERE verb = 'operator_control' ORDER BY id")
    assert json.loads(rows[-1]["params"])["why"] == "the window disconnected"


async def test_the_window_coming_back_inside_the_grace_keeps_control(world, monkeypatch):
    monkeypatch.setattr(boxdesk, "GRACE_S", 0.3)
    box = world["box"]
    await boxdesk.ensure(box)
    first = Recorder()
    boxdesk.viewer_up(box.id, VIEWER, first)
    await boxdesk.take(box, VIEWER, "grant")
    boxdesk.viewer_down(box.id, VIEWER, first)
    await asyncio.sleep(0.1)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())                    # reconnected, same window id
    await asyncio.sleep(0.5)
    assert boxdesk.control_state(box.id)["holder"] == "operator"
    # another window dropping never touches the holder's control
    boxdesk.viewer_up(box.id, OTHER, o := Recorder())
    boxdesk.viewer_down(box.id, OTHER, o)
    await asyncio.sleep(0.4)
    assert boxdesk.control_state(box.id)["holder"] == "operator"


async def test_there_is_no_idle_hand_back(world, monkeypatch):
    """The operator decided: only [Hand back] or the window going away ends it."""
    monkeypatch.setattr(boxdesk, "GRACE_S", 0.05)
    box = world["box"]
    await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    await boxdesk.take(box, VIEWER, "grant")
    await asyncio.sleep(0.4)                                          # no input, window still open
    assert boxdesk.control_state(box.id)["holder"] == "operator"


# --- audit: counts only, never content --------------------------------------------------

async def test_the_audit_rows_carry_counts_never_content(world):
    box = world["box"]
    d = await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    await boxdesk.take(box, VIEWER, "grant")
    for _ in range(3):
        assert boxdesk.admit(box.id, VIEWER, "key")
    for _ in range(5):
        assert boxdesk.admit(box.id, VIEWER, "pointer")
    assert not boxdesk.admit(box.id, VIEWER, "cut_text")             # the clipboard, never
    assert not boxdesk.admit(box.id, OTHER, "key")                   # not the holder's socket
    await boxdesk.hand_back(box.id, "the operator pressed Hand back")
    rows = await _rows("SELECT verb, params, ok, approver, device_id, conversation_id, op_id "
                       "FROM desk_actions WHERE verb = 'operator_control' ORDER BY id")
    assert [json.loads(r["params"])["phase"] for r in rows] == ["start", "end"]
    start, end = (json.loads(r["params"]) for r in rows)
    assert start == {"phase": "start", "box": box.id, "by": "grant"}
    assert set(end) == {"phase", "box", "by", "why", "seconds", "keys", "pointers"}
    assert (end["keys"], end["pointers"]) == (3, 5) and end["why"] == "the operator pressed Hand back"
    assert all(r["ok"] == 1 and r["approver"] == "grant" and r["device_id"] == d.device_id
               for r in rows)
    # and the counters are gone with the take-over: the next one starts at zero
    await boxdesk.take(box, VIEWER, "grant")
    assert boxdesk._control[box.id].keys == 0


async def test_a_quiet_by_operator_event_for_the_take_over(world):
    box = world["box"]
    await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    await boxdesk.take(box, VIEWER, "grant")
    await boxdesk.hand_back(box.id)
    evs = await _events("desk_operator_control")
    assert len(evs) == 1                                             # the take-over, not the hand back
    ev = evs[0]
    assert ev["severity"] == "info" and ev["actor"] == "operator"
    assert ev["acknowledged"] and ev["quiet"] == "operator"           # "by you": filed quiet
    assert json.loads(ev["detail"])["phase"] == "start" and box.id in ev["summary"]


# --- the shared stream -----------------------------------------------------------------

async def test_control_and_the_agents_activity_ride_the_vm_boxes_stream(world):
    box = world["box"]
    await boxdesk.ensure(box)
    boxdesk.viewer_up(box.id, VIEWER, Recorder())
    q = bus.subscribe(boxes.BUS_CHAN)
    try:
        tok = runtime.conversation_id.set(42)
        try:
            with turn("op-a", box, "game"):
                await _tool("desk_screenshot")(computer="sandbox")
        finally:
            runtime.conversation_id.reset(tok)
        await boxdesk.take(box, VIEWER, "grant")
        await boxdesk.hand_back(box.id)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        bus.unsubscribe(boxes.BUS_CHAN, q)
    assert {"type": "display", "box_id": box.id,
            "agent": {"active_age_s": 0, "turns": [42]}} in events
    controls = [e["control"] for e in events if e.get("control")]
    assert [c["holder"] for c in controls] == ["operator", "agent"]
    assert controls[0]["viewer"] == VIEWER and controls[0]["by"] == "grant"
    assert all(e["box_id"] == box.id for e in events)
    st = await display_api.status(box, ask_guest=False)
    assert st["control"]["holder"] == "agent" and st["agent"]["turns"] == [42]
    assert st["agent"]["active_age_s"] is not None and "watch_only" not in st


# --- the RFB filter, third lock ---------------------------------------------------------

def test_the_filter_remembers_what_it_passed_as_pressed_and_lets_go_of_it():
    seen = []
    flt = display_api.RfbInputFilter(lambda kind: seen.append(kind) or True)
    flt.feed(HANDSHAKE + key(1, 0xFFE1) + key(1, 0x61) + key(0, 0x61) + QEMU_KEY + pointer(7, 9, 1))
    assert flt.held_keys == {0xFFE1, 0x61}               # shift, and the QEMU 'a' (down) again
    assert flt.held_buttons == 1
    out = flt.release_bytes()
    assert out == (key(0, 0x61) + key(0, 0xFFE1) + pointer(7, 9, 0)), out
    assert flt.held_keys == set() and flt.release_bytes() == b""
    # a dropped message is never remembered: nothing was pressed in the box
    never = display_api.RfbInputFilter()
    never.feed(HANDSHAKE + key(1, 0x61) + pointer(1, 1, 1))
    assert never.release_bytes() == b""


class _ClosingGuest(FakeGuest):
    """A FakeGuest that hangs up when done: a filter that dropped too much fails
    the test (EOF to the browser) instead of hanging it."""

    def run(self):
        try:
            super().run()
        finally:
            self.sock.close()


def _guest(**kw):
    """(the host's end, a started fake guest on the other end)."""
    host_end, guest_end = socket.socketpair()
    host_end.setblocking(False)
    g = _ClosingGuest(guest_end, **kw)
    g.start()
    return host_end, g


def _wire(monkeypatch, *ends):
    ends = list(ends)

    async def connect(self, port):
        return ends.pop(0)
    monkeypatch.setattr(boxes.VsockTransport, "connect", connect)


def _wait(cond, secs=5):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_only_the_holders_socket_gets_input_through_and_a_hand_back_lets_go(host, monkeypatch):
    b = _desktop_box()
    held = key(1, 0x62) + pointer(5, 6, 1)
    release = key(0, 0x62) + pointer(5, 6, 0)
    total1 = HANDSHAKE + FBU + held + release + FBU
    e1, g1 = _guest(expect=len(total1))
    e2, g2 = _guest(expect=len(HANDSHAKE) + len(FBU))
    _wire(monkeypatch, e1, e2)
    url = f"/api/vm/boxes/{b.id}/display/ws"
    ctl = f"/api/vm/boxes/{b.id}/display/control"
    with host:
        host.portal.call(init_db)
        with host.websocket_connect(f"{url}?viewer={VIEWER}", subprotocols=["binary"]) as w1:
            w1.receive_bytes()
            with host.websocket_connect(f"{url}?viewer={OTHER}", subprotocols=["binary"]) as w2:
                w2.receive_bytes()
                w1.send_bytes(HANDSHAKE)
                w2.send_bytes(HANDSHAKE)
                # nobody holds it yet: a modified client's input goes nowhere
                w1.send_bytes(key(1, 0x61) + pointer(1, 1, 1) + cut_text() + FBU)
                assert _wait(lambda: g1.got == HANDSHAKE + FBU), g1.got
                # the control route refuses what it must
                r = host.post(ctl, json={"holder": "operator", "viewer": "never-connected"})
                assert r.status_code == 409 and "not connected" in r.json()["detail"]
                r = host.post(ctl, json={"holder": "operator", "viewer": VIEWER})
                assert r.status_code == 200 and r.json()["control"]["holder"] == "operator"
                r = host.post(ctl, json={"holder": "operator", "viewer": OTHER})
                assert r.status_code == 409 and "another window" in r.json()["detail"]
                # the holder's input reaches the box; the watcher's, the clipboard and a
                # resize do not
                w1.send_bytes(held + cut_text() + RESIZE)
                w2.send_bytes(key(1, 0x63) + pointer(2, 2, 1) + cut_text() + RESIZE)
                w2.send_bytes(FBU)
                assert w2.receive_bytes() == b"FRAME-1"      # the watcher's stream is in step
                # hand back: held keys are let go, then this window's input is dropped again
                r = host.post(ctl, json={"holder": "agent"})
                assert r.status_code == 200 and r.json()["control"]["holder"] == "agent"
                w1.send_bytes(key(1, 0x64) + pointer(9, 9, 1))
                w1.send_bytes(FBU)
                assert w1.receive_bytes() == b"FRAME-1"
        assert g1.done.wait(5) and g2.done.wait(5)
        assert g1.got == total1, g1.got
        assert g2.got == HANDSHAKE + FBU, g2.got              # none of the watcher's input
        rows = host.portal.call(_rows, "SELECT params FROM desk_actions "
                                "WHERE verb = 'operator_control' ORDER BY id")
        end = json.loads(rows[-1]["params"])
        assert end["phase"] == "end" and (end["keys"], end["pointers"]) == (1, 1)


def test_the_holders_window_closing_hands_back_after_the_grace_and_lets_go(host, monkeypatch):
    monkeypatch.setattr(boxdesk, "GRACE_S", 0.2)
    b = _desktop_box()
    e1, g1 = _guest(expect=10 ** 9)
    _wire(monkeypatch, e1)
    url = f"/api/vm/boxes/{b.id}/display/ws"
    with host:
        host.portal.call(init_db)
        with host.websocket_connect(f"{url}?viewer={VIEWER}", subprotocols=["binary"]) as w1:
            w1.receive_bytes()
            w1.send_bytes(HANDSHAKE)
            r = host.post(f"/api/vm/boxes/{b.id}/display/control",
                          json={"holder": "operator", "viewer": VIEWER})
            assert r.status_code == 200
            w1.send_bytes(key(1, 0x62))
            assert _wait(lambda: boxdesk._control[b.id].keys == 1)
        # the window is gone: still the operator's for the grace, then the agent's
        assert _wait(lambda: boxdesk.control_state(b.id)["holder"] == "agent")
        assert g1.done.wait(5)
        assert g1.got == HANDSHAKE + key(1, 0x62) + key(0, 0x62)    # let go on the way out
        assert _wait(lambda: len(host.portal.call(
            _rows, "SELECT id FROM desk_actions WHERE verb = 'operator_control'")) == 2)
        rows = host.portal.call(_rows, "SELECT params FROM desk_actions "
                                "WHERE verb = 'operator_control' ORDER BY id")
        assert json.loads(rows[-1]["params"])["why"] == "the window disconnected"


def test_a_viewer_that_names_no_id_can_watch_but_never_take(host, monkeypatch):
    b = _desktop_box()
    e1, g1 = _guest(expect=len(HANDSHAKE) + len(FBU))
    _wire(monkeypatch, e1)
    with host:
        host.portal.call(init_db)
        with host.websocket_connect(f"/api/vm/boxes/{b.id}/display/ws",
                                    subprotocols=["binary"]) as w:
            w.receive_bytes()
            r = host.post(f"/api/vm/boxes/{b.id}/display/control", json={"holder": "operator"})
            assert r.status_code == 409
            w.send_bytes(HANDSHAKE + key(1, 0x61) + FBU)
            assert w.receive_bytes() == b"FRAME-1"
        assert g1.done.wait(5) and g1.got == HANDSHAKE + FBU
    assert boxdesk.control_state(b.id)["holder"] == "agent"
