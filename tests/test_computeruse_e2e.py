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

    async def grants(db=None):
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
    assert "granted folders" in out
    assert str(wired["music"]) in out
    assert "dry-run" in out


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
