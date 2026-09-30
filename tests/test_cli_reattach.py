"""The terminal client when the connection drops mid-turn (clients/jav3cli/jav3): it
keeps retrying quietly, re-attaches to a turn that is still running, and draws what
a finished turn did while it was away, each row once. And /logout in two steps."""
import httpx
import pytest

from cli_fake import FakeServer, call, finish, load_client, open_chat, send, wait_for

jav3 = load_client("jav3cli_reattach")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(jav3, "RECONNECT_STEPS", (0.05,), raising=False)
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


# --- /logout: who, then yes ------------------------------------------------------------------

async def test_a_bare_logout_only_says_who_and_what_yes_does():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/logout")
        assert await wait_for(lambda: any("/logout yes" in n for n in notes(app)))
        line = next(n for n in notes(app) if "/logout yes" in n)
        assert "signed in as device:test" in line and "revokes this computer's token" in line
        assert app.token == "jvd_x" and app.logged_in is not False
        assert app.screen_stack[-1] is app.screen_stack[0]        # no dialog either
        await send(pilot, app, "/logout maybe")                   # anything but yes is still step one
        await pilot.pause(0.3)
        assert app.token == "jvd_x"


async def test_logout_yes_signs_out():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/logout yes")
        assert await wait_for(lambda: app.token is None)
        assert app.logged_in is False
