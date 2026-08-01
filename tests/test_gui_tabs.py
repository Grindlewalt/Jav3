"""Music plays on ONE machine — the one that asked.

The operator's report: "playing music, it plays on all connected computers".
It did. Every open Jarvis tab was an anonymous subscriber on one broadcast
channel, so a play event went to the laptop, the desktop and the phone at once,
all slightly out of sync, and there was no way to say which was meant because
none of them had a name.

These tests are about addressing: a tab has an identity, a push can name one,
and the default is the tab the turn came from.
"""
import pytest

from backend import bus, gui, runtime


@pytest.fixture
def tabs():
    """Three open tabs, each with its own queue, cleaned up afterwards."""
    made = {}
    for tab_id, name in (("t-mac", "Mac · Chrome"),
                         ("t-desk", "Desktop · Firefox"),
                         ("t-phone", "iPhone · Safari")):
        q = bus.subscribe(gui.GUI_CHAN)
        gui.register_tab(tab_id, name, q)
        made[tab_id] = q
    yield made
    for tab_id, q in made.items():
        gui.forget_tab(tab_id)
        bus.unsubscribe(gui.GUI_CHAN, q)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_push_can_name_one_tab(tabs):
    assert gui.push({"type": "player", "action": "play"}, tab="t-mac") == 1
    assert len(_drain(tabs["t-mac"])) == 1
    assert _drain(tabs["t-desk"]) == [], "this is the bug: the desktop played too"
    assert _drain(tabs["t-phone"]) == []


def test_an_unaddressed_push_still_reaches_everyone(tabs):
    """Toasts and layout changes are genuinely for every tab; only the ones that
    make noise became per-tab."""
    assert gui.push({"type": "layout_changed", "slug": "x"}) == 3
    for q in tabs.values():
        assert len(_drain(q)) == 1


def test_a_closed_tab_does_not_fall_back_to_all_of_them(tabs):
    """Falling back to a broadcast is the behaviour being removed — it would
    turn "that tab is gone" back into "every computer starts playing"."""
    assert gui.push({"type": "player"}, tab="t-gone") == 0
    for q in tabs.values():
        assert _drain(q) == []


def test_the_asking_tab_wins(tabs):
    got, name = gui.resolve_tab(None, "t-phone")
    assert (got, name) == ("t-phone", "iPhone · Safari")


def test_a_named_tab_beats_the_asking_one(tabs):
    """"Put it on the mac" from the phone."""
    got, name = gui.resolve_tab("mac", "t-phone")
    assert got == "t-mac"
    got, _ = gui.resolve_tab("Mac · Chrome", "t-phone")
    assert got == "t-mac"


def test_an_unknown_name_lists_what_is_open_instead_of_guessing(tabs):
    got, why = gui.resolve_tab("kitchen", "t-phone")
    assert got is None
    assert "Mac · Chrome" in why and "kitchen" in why


def test_an_ambiguous_name_asks(tabs):
    q = bus.subscribe(gui.GUI_CHAN)
    gui.register_tab("t-mac2", "Mac · Safari", q)
    try:
        got, why = gui.resolve_tab("mac", None)
        assert got is None and "several" in why
    finally:
        gui.forget_tab("t-mac2")
        bus.unsubscribe(gui.GUI_CHAN, q)


def test_with_no_asking_tab_the_most_recent_one_is_used(tabs):
    """A schedule or an agent run has no tab. Most recently used beats both
    'nowhere' and 'everywhere'."""
    gui.touch_tab("t-desk")
    got, _ = gui.resolve_tab(None, None)
    assert got == "t-desk"


def test_nothing_open_is_reported_not_broadcast():
    got, why = gui.resolve_tab(None, None)
    assert got is None and "no Jarvis tab is open" in why


@pytest.mark.asyncio
async def test_play_music_lands_in_the_asking_tab_only(tabs, tmp_path, monkeypatch):
    """End to end through the tool, which is where the operator met the bug."""
    monkeypatch.setattr(gui, "media_src", lambda source, slug: ("/x.mp3", None))

    async def slug():
        return "proj"
    from backend.agent.tools import toolctx
    monkeypatch.setattr(toolctx, "active_slug", slug)

    from tools.play_music.handler import run
    token = runtime.gui_tab.set("t-mac")
    try:
        out = await run(source="song.mp3")
    finally:
        runtime.gui_tab.reset(token)

    assert "Mac · Chrome" in out
    assert len(_drain(tabs["t-mac"])) == 1
    assert _drain(tabs["t-phone"]) == []


@pytest.mark.asyncio
async def test_play_music_says_so_when_there_is_nowhere_to_play(monkeypatch):
    monkeypatch.setattr(gui, "media_src", lambda source, slug: ("/x.mp3", None))

    async def slug():
        return "proj"
    from backend.agent.tools import toolctx
    monkeypatch.setattr(toolctx, "active_slug", slug)

    from tools.play_music.handler import run
    out = await run(source="song.mp3")
    assert "no Jarvis tab is open" in out


def test_a_tab_that_disconnects_is_forgotten(tabs):
    gui.forget_tab("t-mac")
    got, why = gui.resolve_tab("mac", None)
    assert got is None, why


def test_the_stream_route_registers_and_forgets(tabs):
    """The registration is tied to the SSE subscription's lifetime, so a closed
    laptop stops being a place music can be sent."""
    import inspect
    src = inspect.getsource(gui.gui_stream)
    assert "register_tab" in src and "forget_tab" in src


def test_chat_carries_the_tab_into_the_turn():
    """Without this the contextvar is never set and every turn falls back to
    'most recently used', which is right for a schedule and wrong for a chat."""
    import inspect
    from backend import chat
    assert "tab" in chat.ChatRequest.model_fields
    assert "runtime.gui_tab.set" in inspect.getsource(chat._run_chat_turn)
