"""/local reads outside the working directory ask like writes do, and the secret
places (keys, tokens, browser profiles, the client's own login, .env files outside
the directory) are refused whatever the operator answers (TUI-08)."""
import socket

import pytest

from cli_fake import FakeServer, finish, load_client, send, top, wait_for

jav3 = load_client("jav3cli_local_reads")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A work directory, a home directory with secrets in it, another folder, and a
    client config directory; HOME and XDG_CONFIG_HOME point at them."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    other = tmp_path / "other"
    for d in (home / ".ssh", home / ".config" / "jav3", work, other):
        d.mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
    (home / ".config" / "jav3" / "credentials.json").write_text('{"token": "jvd_secret"}')
    (home / "notes.txt").write_text("my notes")
    (work / "a.py").write_text("x = 1\n")
    (work / ".env").write_text("KEY=inside-ok")
    (other / "b.txt").write_text("outside one")
    (other / "c.txt").write_text("outside two")
    (other / ".env").write_text("SECRET=outside")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return {"home": home, "work": work, "other": other}


def executor(world, answers=None):
    asked = []

    def approve(kind, summary):
        asked.append((kind, summary))
        return (answers or {}).get(kind, "yes")
    return jav3.LocalExecutor(world["work"], approve), asked


async def test_a_read_outside_the_directory_asks_and_says_where(world):
    ex, asked = executor(world)
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / "b.txt")})
    assert ok and "outside one" in out
    assert [k for k, _ in asked] == ["read"]
    assert str(world["other"] / "b.txt") in asked[0][1] and str(world["work"]) in asked[0][1]
    # inside the directory nothing asks
    ok, out = await ex.run("local_read_file", {"path": "a.py"})
    assert ok and len(asked) == 1


async def test_declined_read_returns_nothing_but_the_refusal(world):
    ex, asked = executor(world, {"read": "no"})
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / "b.txt")})
    assert not ok and "did not approve" in out and "outside one" not in out


async def test_relative_and_home_paths_outside_ask_too(world):
    ex, asked = executor(world)
    ok, _ = await ex.run("local_read_file", {"path": "../other/b.txt"})
    assert ok and len(asked) == 1
    ok, out = await ex.run("local_read_file", {"path": "~/notes.txt"})
    assert ok and "my notes" in out and len(asked) == 2


async def test_a_symlink_out_of_the_directory_is_an_outside_read(world):
    (world["work"] / "link.txt").symlink_to(world["other"] / "b.txt")
    ex, asked = executor(world)
    ok, out = await ex.run("local_read_file", {"path": "link.txt"})
    assert ok and "outside one" in out and [k for k, _ in asked] == ["read"]


async def test_always_remembers_the_folder_not_every_folder(world):
    ex, asked = executor(world, {"read": "always"})
    ok, _ = await ex.run("local_read_file", {"path": str(world["other"] / "b.txt")})
    assert ok and len(asked) == 1
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / "c.txt")})
    assert ok and "outside two" in out and len(asked) == 1        # same folder: no new ask
    ok, _ = await ex.run("local_read_file", {"path": str(world["home"] / "notes.txt")})
    assert ok and len(asked) == 2                                  # another folder asks again


async def test_listing_and_searching_outside_ask_and_always_covers_beneath(world):
    ex, asked = executor(world, {"read": "always"})
    ok, out = await ex.run("local_list_files", {"path": str(world["other"])})
    assert ok and "b.txt" in out and len(asked) == 1
    ok, out = await ex.run("local_search", {"query": "outside", "path": str(world["other"])})
    assert ok and "b.txt" in out and len(asked) == 1               # covered by the listing's folder
    assert ".env" not in out                                       # .env outside is never searched
    sub = world["other"] / "sub"
    sub.mkdir()
    (sub / "d.txt").write_text("outside three")
    ok, out = await ex.run("local_read_file", {"path": str(sub / "d.txt")})
    assert ok and len(asked) == 1


async def test_no_approver_fails_closed_for_outside_reads(world):
    ex = jav3.LocalExecutor(world["work"])
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / "b.txt")})
    assert not ok and "outside" in out and "outside one" not in out
    ok, out = await ex.run("local_read_file", {"path": "a.py"})
    assert ok


@pytest.mark.parametrize("where", ["~/.ssh/id_ed25519", "~/.config/jav3/credentials.json"])
async def test_secret_places_are_refused_and_never_even_asked(world, where):
    ex, asked = executor(world, {"read": "always", "write": "always", "edit": "always"})
    ok, out = await ex.run("local_read_file", {"path": where})
    assert not ok and "never lets an agent touch" in out
    assert "PRIVATE KEY" not in out and "jvd_secret" not in out
    ok, out = await ex.run("local_write_file", {"path": where, "content": "x"})
    assert not ok and "never lets" in out
    ok, out = await ex.run("local_edit_file", {"path": where, "find": "a", "replace": "b"})
    assert not ok and "never lets" in out
    assert asked == []
    assert (world["home"] / ".ssh" / "id_ed25519").read_text() == "PRIVATE KEY"


async def test_secret_places_stay_refused_when_they_are_inside_the_directory(world):
    """Started from the home directory the keys are 'inside': still refused."""
    ex = jav3.LocalExecutor(world["home"], lambda k, s: "always")
    ok, out = await ex.run("local_read_file", {"path": ".ssh/id_ed25519"})
    assert not ok and "PRIVATE KEY" not in out
    ok, out = await ex.run("local_list_files", {"path": ".ssh"})
    assert not ok
    ok, out = await ex.run("local_search", {"query": "PRIVATE"})
    assert "PRIVATE KEY" not in out and ".ssh" not in out
    ok, out = await ex.run("local_list_files", {"path": ".", "depth": 3})
    assert ok and "notes.txt" in out and "id_ed25519" not in out and "credentials.json" not in out


async def test_env_files_outside_are_refused_inside_are_fine(world):
    ex, asked = executor(world, {"read": "always"})
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / ".env")})
    assert not ok and ".env" in out and "SECRET" not in out and asked == []
    ok, out = await ex.run("local_read_file", {"path": ".env"})
    assert ok and "inside-ok" in out and asked == []
    (world["other"] / ".env.example").write_text("SECRET=")
    ok, out = await ex.run("local_read_file", {"path": str(world["other"] / ".env.example")})
    assert ok and [k for k, _ in asked] == ["read"]


async def test_a_write_outside_asks_every_time_even_after_always(world):
    ex, asked = executor(world, {"write": "always"})
    ok, _ = await ex.run("local_write_file", {"path": "new.txt", "content": "1"})
    assert ok and len(asked) == 1
    ok, _ = await ex.run("local_write_file", {"path": "again.txt", "content": "1"})
    assert ok and len(asked) == 1                                  # inside: always holds
    ok, _ = await ex.run("local_write_file", {"path": str(world["other"] / "w.txt"),
                                              "content": "1"})
    assert ok and len(asked) == 2
    assert "outside" in asked[1][1]
    ok, _ = await ex.run("local_write_file", {"path": str(world["other"] / "w2.txt"),
                                              "content": "1"})
    assert ok and len(asked) == 3                                  # outside: asks every time


async def test_the_tui_asks_before_a_local_read_outside(world, monkeypatch):
    """/local, the model asks to read a file outside the directory: the operator
    sees the dialog; declining sends the refusal back, not the file."""
    pytest.importorskip("textual")
    srv = FakeServer()
    srv.local_info = {"cwd": str(world["work"].resolve()), "hostname": socket.gethostname(),
                      "os": "x", "shell": "/bin/sh"}
    monkeypatch.chdir(world["work"])
    target = str(world["other"] / "b.txt")
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/local")
        await pilot.pause(0.2)
        assert app.local is True
        await send(pilot, app, "look around")
        assert await wait_for(lambda: srv.feeds)
        srv.feed.put({"type": "start", "conversation_id": 4},
                     {"type": "local_tool", "id": "c1", "name": "local_read_file",
                      "args": {"path": target}})
        assert await wait_for(lambda: top(app) == "LocalApprove")
        scr = app.screen
        assert scr.kind == "read" and target in scr.summary
        type(scr).GRACE = 0
        await pilot.press("n")
        assert await wait_for(lambda: srv.local_results)
        res = srv.local_results[0]
        assert res["id"] == "c1" and res["ok"] is False and "outside one" not in res["result"]
        await finish(srv, app)
