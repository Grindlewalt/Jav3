"""Tool -> contract -> client -> argv, with the real client on the other end.

The transport is stubbed (the WebSocket is exercised by hand; see the module
docstring in backend/computeruse_api.py) but everything either side of it is the
real thing: the real tool handlers, the real validate(), the real client
clean_params/containment, and the real Runner assembling argv.

The client runs in dry-run throughout, so no test here can make a sound.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from backend import computeruse as cu

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "computeruse" / "agent.py"


def _load_client():
    spec = importlib.util.spec_from_file_location("cu_agent_e2e", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A granted folder, a connected client, and the argv it would have run."""
    music = tmp_path / "Music"
    music.mkdir()
    (music / "Test Track.mp3").write_bytes(b"\0")
    (music / "Другой.flac").write_bytes(b"\0")
    (music / "notes.txt").write_text("not media")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"\0")

    async def grants(db=None, client=None):
        return [cu.Grant(1, str(music), "test")]
    monkeypatch.setattr(cu, "list_grants", grants)

    mod = _load_client()
    agent = mod.Agent("http://x", "t", [str(music)], "testbox", dry_run=True)
    # pretend the usual binaries exist so argv assembly is exercised
    agent.runner.bin.update({"mpv": "/usr/bin/mpv", "pactl": "/usr/bin/pactl",
                             "xdg-open": "/usr/bin/xdg-open"})
    calls = []
    agent.runner.run = lambda name, *a, **k: calls.append([name, *a]) or ""

    class FakeClient(cu.Client):
        pass

    sent = []

    async def send(raw):
        msg = json.loads(raw)
        sent.append(msg)
        try:
            result = agent.handle(msg["verb"], msg["params"])
            reply = {"id": msg["id"], "ok": True, "result": result}
        except Exception as e:                     # Refused or anything else
            reply = {"id": msg["id"], "ok": False, "error": str(e)}
        cu.resolve_result(client.id, msg["id"], reply)

    client = FakeClient(id="testbox", name="testbox", platform="linux", send=send)
    cu.register(client)
    yield {"music": music, "outside": outside, "calls": calls, "sent": sent,
           "agent": agent, "mod": mod}
    cu.unregister("testbox")


# --- the tools ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_volume_tool_reaches_a_mixer_with_an_argv_list(wired):
    from tools.computer_volume.handler import run
    out = await run(action="up", percent=5)
    assert "volume up" in out
    argv = wired["calls"][-1]
    assert argv[0] in ("pactl", "wpctl")     # whichever this box has
    assert all(isinstance(a, str) for a in argv)
    assert not any(";" in a or "|" in a or "&" in a for a in argv)


@pytest.mark.asyncio
async def test_volume_tool_refuses_a_bogus_action(wired):
    from tools.computer_volume.handler import run
    out = await run(action="up; rm -rf /")
    assert out.startswith("error:")
    assert not wired["calls"], "nothing should have been run"


@pytest.mark.asyncio
async def test_play_finds_a_track_in_a_granted_folder(wired):
    from tools.computer_play.handler import run
    out = await run(query="test track", kind="audio")
    assert "playing" in out
    argv = wired["calls"][-1]
    assert argv[0] == "mpv"
    assert "--no-config" in argv and "--load-scripts=no" in argv
    assert argv[-1] == str(wired["music"] / "Test Track.mp3")
    assert argv[-2] == "--", "the filename must sit after -- so it can't be an option"


@pytest.mark.asyncio
async def test_play_refuses_a_path_outside_the_grant(wired):
    from tools.computer_play.handler import run
    out = await run(path=str(wired["outside"]), kind="audio")
    assert out.startswith("error:")
    assert "granted" in out
    assert not wired["calls"]


@pytest.mark.asyncio
async def test_play_refuses_a_non_media_file_in_a_granted_folder(wired):
    from tools.computer_play.handler import run
    out = await run(path=str(wired["music"] / "notes.txt"), kind="audio")
    assert out.startswith("error:")
    assert not wired["calls"]


@pytest.mark.asyncio
async def test_play_lists_candidates_instead_of_guessing(wired):
    from tools.computer_play.handler import run
    out = await run(query="", kind="audio")           # no query at all
    assert out.startswith("error:")
    (wired["music"] / "Test Track 2.mp3").write_bytes(b"\0")
    out = await run(query="test", kind="audio")
    assert "match" in out and "Test Track" in out
    assert not wired["calls"], "ambiguous matches must not start playback"


@pytest.mark.asyncio
async def test_play_passes_screen_and_device_through(wired):
    from tools.computer_play.handler import run
    out = await run(query="Другой", kind="video")   # a .flac is not a video
    assert "playing" not in out and not wired["calls"]

    (wired["music"] / "Film.mkv").write_bytes(b"\0")
    out = await run(query="film", kind="video", screen=1,
                    device="alsa_output.pci-0000_00_1f.3.analog-stereo", volume=30)
    assert "screen 1" in out
    argv = wired["calls"][-1]
    assert "--screen=1" in argv
    assert "--audio-device=alsa_output.pci-0000_00_1f.3.analog-stereo" in argv
    assert "--volume=30" in argv
    assert "--fullscreen" in argv


@pytest.mark.asyncio
async def test_open_link_tool_only_accepts_http(wired):
    from tools.computer_open_link.handler import run
    assert "opened" in await run(url="https://example.com")
    assert wired["calls"][-1][0] == "xdg-open"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ssh://host"):
        before = len(wired["calls"])
        out = await run(url=bad)
        assert out.startswith("error:"), bad
        assert len(wired["calls"]) == before, f"{bad} reached the OS"


@pytest.mark.asyncio
async def test_status_tool_reports_the_granted_roots(wired):
    from tools.computer_status.handler import run
    out = await run()
    assert "folders reachable" in out
    assert str(wired["music"]) in out
    assert "DRY RUN" in out


@pytest.mark.asyncio
async def test_a_title_cannot_smuggle_control_characters_into_argv(wired):
    """The filename becomes --force-media-title=... in argv; a terminal escape
    in there is the classic way to make a log line lie."""
    (wired["music"] / "ok\x07bell.mp3").write_bytes(b"\0")
    from tools.computer_play.handler import run
    out = await run(query="bell", kind="audio")
    # either refused, or the title was dropped -- never passed through
    for argv in wired["calls"]:
        for a in argv:
            assert "\x07" not in str(a), out


# --- no client connected -----------------------------------------------------

@pytest.mark.asyncio
async def test_tools_say_so_when_nothing_is_connected():
    from tools.computer_volume.handler import run
    out = await run(action="up")
    assert "no computer-use client is connected" in out


# --- browsing a library ------------------------------------------------------

@pytest.mark.asyncio
async def test_library_lists_folders_with_counts_and_files(wired, monkeypatch):
    """Walked on the client, because the Jarvis host cannot see that disk."""
    music = wired["music"]
    (music / "Action").mkdir()
    (music / "Action" / "Heat.mkv").write_bytes(b"\0")
    (music / "Action" / "Ronin.mp4").write_bytes(b"\0")
    (music / "Dune.mp4").write_bytes(b"\0")

    from tools.computer_library.handler import run
    out = await run(kind="video")
    assert "Action/" in out and "(2 files)" in out
    assert "Dune.mp4" in out
    assert "notes.txt" not in out, "non-media must not be listed"

    deeper = await run(folder="Action", kind="video")
    assert "Heat.mkv" in deeper and "Ronin.mp4" in deeper


@pytest.mark.asyncio
async def test_library_refuses_a_folder_outside_the_grants(wired):
    from tools.computer_library.handler import run
    for bad in ("/etc", str(wired["outside"].parent)):
        out = await run(folder=bad)
        assert "error" in out.lower(), bad
    # '..' is refused by the contract before it reaches the filesystem
    for bad in ("..", "../.."):
        out = await run(folder=bad)
        assert "error" in out.lower(), bad


@pytest.mark.asyncio
async def test_library_caps_a_huge_listing(wired):
    """A film library is thousands of files; dumping it wastes the context the
    actual task needs."""
    for i in range(120):
        (wired["music"] / f"track{i:03d}.mp3").write_bytes(b"\0")
    from tools.computer_library.handler import run
    out = await run(kind="audio", limit=20)
    assert "and " in out and "more here" in out
    assert out.count("track") <= 25


# --- device resolution removes the preflight ---------------------------------

@pytest.mark.asyncio
async def test_a_device_name_is_resolved_to_a_real_id(wired):
    """The point of this: no computer_status round trip before acting."""
    agent = wired["agent"]
    agent.os._cache["sinks"] = (9e18, [
        {"id": "alsa_output.pci-0000_00_1f.3.analog-stereo",
         "label": "Built-in Audio Analog Stereo"},
        {"id": "alsa_output.usb-Focusrite", "label": "Scarlett Desk Speakers"}])
    from tools.computer_volume.handler import run
    out = await run(action="up", percent=5, device="desk speakers")
    assert not out.startswith("error"), out
    argv = wired["calls"][-1]
    # what reached argv is the machine's own id, never the model's phrase
    assert "alsa_output.usb-Focusrite" in argv
    assert not any("desk speakers" in str(a) for a in argv)


@pytest.mark.asyncio
async def test_an_unmatched_device_error_lists_the_options(wired):
    """So the model can correct itself from the error instead of preflighting."""
    agent = wired["agent"]
    agent.os._cache["sinks"] = (9e18, [{"id": "sink.one", "label": "One"},
                                       {"id": "sink.two", "label": "Two"}])
    from tools.computer_volume.handler import run
    out = await run(action="up", device="headphones")
    assert out.startswith("error")
    assert "sink.one" in out and "sink.two" in out


@pytest.mark.asyncio
async def test_an_ambiguous_device_asks_rather_than_guessing(wired):
    agent = wired["agent"]
    agent.os._cache["sinks"] = (9e18, [{"id": "hdmi.a", "label": "HDMI 1"},
                                       {"id": "hdmi.b", "label": "HDMI 2"}])
    from tools.computer_volume.handler import run
    out = await run(action="up", device="hdmi")
    assert "matches several" in out


def test_enumeration_is_cached_so_repeat_calls_cost_nothing(wired):
    """Each list costs a subprocess, and Jarvis asks far more often than they
    change."""
    agent = wired["agent"]
    calls = []
    agent.os._cache.clear()
    agent.os._audio_devices = lambda: calls.append(1) or [{"id": "x", "label": "X"}]
    for _ in range(5):
        agent.os.audio_devices()
    assert len(calls) == 1, f"enumerated {len(calls)} times, should be cached"


@pytest.mark.asyncio
async def test_an_unenumerable_machine_still_accepts_a_device_id(wired):
    """"We could not enumerate" is not "that device does not exist". Refusing
    when the list is empty would make a perfectly good id unusable any time mpv
    or pactl is missing, or the query fails."""
    agent = wired["agent"]
    agent.os._cache["outputs"] = (9e18, [])          # enumeration came back empty
    (wired["music"] / "Film.mkv").write_bytes(b"\0")
    from tools.computer_play.handler import run
    out = await run(query="film", kind="video",
                    device="pulse/alsa_output.pci-0000_00_1f.3.analog-stereo")
    assert not out.startswith("error"), out
    argv = wired["calls"][-1]
    assert "--audio-device=pulse/alsa_output.pci-0000_00_1f.3.analog-stereo" in argv


@pytest.mark.asyncio
async def test_but_a_known_list_still_refuses_an_unknown_device(wired):
    """The permissive path above must only apply when we know nothing."""
    agent = wired["agent"]
    agent.os._cache["outputs"] = (9e18, [{"id": "pulse/real", "label": "Real"}])
    (wired["music"] / "Film2.mkv").write_bytes(b"\0")
    from tools.computer_play.handler import run
    out = await run(query="film2", kind="video", device="pulse/invented")
    assert out.startswith("error") and "pulse/real" in out


# --- the host/client filesystem split ----------------------------------------

@pytest.mark.asyncio
async def test_playing_a_path_that_exists_only_on_the_client(tmp_path, monkeypatch):
    """The bug this whole split fixes.

    The Jarvis host used to resolve media paths against ITS disk, so a grant for
    /Users/you/Movies could not even be created (is_dir() on the Pi) and nothing
    on a laptop was ever playable. The host's check is lexical now; the client
    checks the real file.
    """
    # a "remote" root the host cannot see, standing in for /Users/you/Movies
    remote = tmp_path / "Users" / "you" / "Movies"
    remote.mkdir(parents=True)
    (remote / "Heat.mkv").write_bytes(b"\0")

    async def grants(db=None, client=None):
        return [cu.Grant(1, str(remote), "mac films")]
    monkeypatch.setattr(cu, "list_grants", grants)

    mod = _load_client()
    agent = mod.Agent("http://x", "t", [str(remote)], "mac", dry_run=True)
    agent.runner.bin["mpv"] = "/usr/bin/mpv"
    calls = []
    agent.runner.run = lambda name, *a, **k: calls.append([name, *a]) or ""

    sent = []

    async def send(raw):
        msg = json.loads(raw)
        sent.append(msg)
        try:
            reply = {"id": msg["id"], "ok": True,
                     "result": agent.handle(msg["verb"], msg["params"])}
        except Exception as e:
            reply = {"id": msg["id"], "ok": False, "error": str(e)}
        cu.resolve_result("mac", msg["id"], reply)

    cu.register(cu.Client(id="mac", name="mac", platform="darwin", send=send))
    try:
        from tools.computer_library.handler import run as lib
        out = await lib(kind="video")
        assert "Heat.mkv" in out, out

        from tools.computer_play.handler import run as play
        out = await play(query="heat", kind="video")
        assert "playing" in out, out
        assert str(remote / "Heat.mkv") in calls[-1]
    finally:
        cu.unregister("mac")


@pytest.mark.asyncio
async def test_a_mac_style_grant_can_be_created(monkeypatch, tmp_path):
    """add_grant used to require the directory to exist on the Jarvis host, so
    every /Users/... grant was rejected outright."""
    import backend.db as db_mod
    monkeypatch.setattr(db_mod.settings, "db_path", tmp_path / "t.db", raising=False)
    await db_mod.init_db()
    g = await cu.add_grant("/Users/grant/Movies", "mac films")
    assert g.root == "/Users/grant/Movies"
    assert not Path(g.root).exists(), "the point is it is not on this host"
    await cu.remove_grant(g.id)


@pytest.mark.asyncio
async def test_a_tilde_grant_is_refused_rather_than_silently_useless(
        monkeypatch, tmp_path):
    """"~/Movies" looks accepted and then matches nothing: this host cannot
    expand a home directory on another machine, and the client resolves "~"
    against its working directory rather than $HOME. Observed live — a grant
    stored as "~/Movies" that no client could ever use."""
    import backend.db as db_mod
    monkeypatch.setattr(db_mod.settings, "db_path", tmp_path / "t.db", raising=False)
    await db_mod.init_db()
    for bad in ("~/Movies", "~", "~grant/Movies", "  ~/Music  "):
        with pytest.raises(cu.VerbError) as e:
            await cu.add_grant(bad)
        assert "full path" in str(e.value), bad
    # the spelled-out form is fine
    g = await cu.add_grant("/Users/grant/Movies")
    assert g.root == "/Users/grant/Movies"
    await cu.remove_grant(g.id)
