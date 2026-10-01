"""The terminal client updates itself from its server: GET /cli/version says which
client file the server serves; the TUI notices (quietly, at most one request an hour)
that it differs from its own; /update and `jav3 update` swap the file, with a backup,
and refuse when they must (a checkout, a read-only install, a download that is not
Python, libraries that are too old)."""
import argparse
import asyncio
import hashlib
import io
import os
import time

import httpx
import pytest

from cli_fake import FakeServer, load_client, send, wait_for

jav3 = load_client("jav3cli_update")

OLD = "# the installed client\nVERSION = 1\n"
NEW = "# the server's client\nVERSION = 2\n\ndef main():\n    return 0\n"
BASE = "http://h:1"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """An installed client (not in a checkout) beside a launcher and a venv it must leave alone."""
    share = tmp_path / "share" / "jav3"
    (share / "venv" / "bin").mkdir(parents=True)
    (share / "venv" / "bin" / "python").write_text("venv python")
    (share / "launcher").write_text("#!/bin/sh\nexec python jav3\n")
    path = share / "jav3"
    path.write_text(OLD)
    monkeypatch.setattr(jav3, "_client_file", lambda: path)
    return path


def untouched(path, others=("launcher", "venv/bin/python")):
    assert path.read_text() == OLD
    assert sorted(p.name for p in path.parent.iterdir()) == ["jav3", "launcher", "venv"]
    assert [(path.parent / o).read_text() for o in others] == ["#!/bin/sh\nexec python jav3\n",
                                                               "venv python"]


class UpdSrv(FakeServer):
    """A FakeServer that also serves /cli/jav3 and /cli/version."""

    def __init__(self, served: str = NEW, requires=(), **kw):
        super().__init__(**kw)
        self.served = served
        self.requires = list(requires)
        self.version_status = 200
        self.gate: asyncio.Event | None = None     # hold /cli/version until the test sets it

    def handle(self, request):
        path = request.url.path
        if path == "/cli/version":
            self.calls.append((request.method, path))
            if self.down:
                raise httpx.ConnectError("All connection attempts failed")
            if self.version_status != 200:
                return httpx.Response(self.version_status, json={"detail": "nope"})
            return httpx.Response(200, json={
                "sha256": sha(self.served), "size": len(self.served.encode()),
                "mtime": 1, "requires": self.requires})
        if path == "/cli/jav3":
            self.calls.append((request.method, path))
            return httpx.Response(200, text=self.served)
        return super().handle(request)

    def transport(self):
        if self.gate is None:                  # sync: /update's download runs in a thread
            return super().transport()

        async def slow(request):
            if self.gate is not None and request.url.path == "/cli/version":
                await self.gate.wait()
            return self.handle(request)
        return httpx.MockTransport(slow)


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


# --- the server's side ------------------------------------------------------------------

async def test_version_endpoint_matches_the_file_it_serves_and_needs_no_login(tmp_env):
    from backend.db import init_db
    from backend.main import app
    await init_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://jav3.lan:8000") as anon:
        file = await anon.get("/cli/jav3")
        v = await anon.get("/cli/version")
        # the same access as the file: no cookie, no token
        assert file.status_code == 200 and v.status_code == 200
        body = v.json()
        assert body["sha256"] == hashlib.sha256(file.content).hexdigest()
        assert body["size"] == len(file.content) and isinstance(body["mtime"], int)
        # the pins install.sh installs, for the client to hold against its libraries
        assert any(p.startswith("httpx>=") for p in body["requires"])
        assert any(p.startswith("textual>=") for p in body["requires"])
        assert "no-store" in v.headers["cache-control"]


def test_version_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    from backend import devices_api as d
    (tmp_path / "jav3").write_text(OLD)
    (tmp_path / "install.sh").write_text("HTTPX_SPEC='httpx>=0.27,<1'\nTUI_SPECS='textual>=8,<9'\n")
    monkeypatch.setattr(d, "CLI_DIR", tmp_path)
    monkeypatch.setattr(d, "_cli_version_cache", {"key": None, "body": None})
    a = d._cli_version()
    assert a["sha256"] == sha(OLD) and a["requires"] == ["httpx>=0.27,<1", "textual>=8,<9"]
    assert d._cli_version() is a                       # unchanged file: the same answer, no re-hash
    (tmp_path / "jav3").write_text(NEW)
    st = (tmp_path / "jav3").stat()
    os.utime(tmp_path / "jav3", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    b = d._cli_version()
    assert b is not a and b["sha256"] == sha(NEW) and b["size"] == len(NEW)
    assert b["mtime"] >= a["mtime"]
    # same mtime, different size (a quick edit within the timestamp's granularity)
    (tmp_path / "jav3").write_text(NEW + "x = 1\n")
    os.utime(tmp_path / "jav3", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert d._cli_version()["sha256"] == sha(NEW + "x = 1\n")


# --- helpers ------------------------------------------------------------------------------

def test_a_checkout_is_the_repo_layout_under_a_git_dir_not_any_git_ancestor(tmp_path):
    repo = tmp_path / "work" / "Jav3"
    (repo / ".git").mkdir(parents=True)
    (repo / "clients" / "jav3cli").mkdir(parents=True)
    assert jav3._in_checkout(repo / "clients" / "jav3cli" / "jav3")
    (tmp_path / "work" / "wt" / "clients" / "jav3cli").mkdir(parents=True)
    (tmp_path / "work" / "wt" / ".git").write_text("gitdir: elsewhere")     # a worktree
    assert jav3._in_checkout(tmp_path / "work" / "wt" / "clients" / "jav3cli" / "jav3")
    # a dotfiles repo around the home directory does not make the installed copy a checkout
    (tmp_path / ".git").mkdir()
    assert not jav3._in_checkout(tmp_path / ".local" / "share" / "jav3" / "jav3")


@pytest.mark.parametrize("version, spec, ok", [
    ("8.0.1", ">=8,<9", True), ("8", ">=8,<9", True), ("7.9", ">=8,<9", False),
    ("9.0", ">=8,<9", False), ("0.28.1", ">=0.27,<1", True), ("0.26.0", ">=0.27,<1", False),
    ("1.0.0", ">=0.27,<1", False), ("8.1.0rc1", ">=8,<9", True), ("2", "", True)])
def test_spec_check(version, spec, ok):
    assert jav3._spec_ok(version, spec) is ok


def test_lib_problems_name_only_installed_libraries_that_are_too_old(monkeypatch):
    from importlib import metadata
    have = {"httpx": "0.27.2", "textual": "7.1.0"}

    def fake(name):
        if name not in have:
            raise metadata.PackageNotFoundError(name)
        return have[name]
    monkeypatch.setattr(metadata, "version", fake)
    assert jav3._lib_problems(["httpx>=0.27,<1", "textual>=8,<9"]) == ["textual 7.1.0 (needs >=8,<9)"]
    assert jav3._lib_problems(["rich>=99"]) == []               # not installed: not a problem
    assert jav3._lib_problems(None) == [] and jav3._lib_problems("nonsense") == []


# --- the check: compare, throttle ---------------------------------------------------------

async def test_check_asks_once_an_hour_and_remembers_the_answer(cfg):
    srv = UpdSrv()
    t = srv.transport()
    ask = lambda base, now: jav3._server_client_sha(base, t, now)    # noqa: E731
    assert await ask(BASE, 1000.0) == sha(NEW)
    assert srv.calls.count(("GET", "/cli/version")) == 1
    assert await ask(BASE, 1000.0 + 3599) == sha(NEW)                 # within the hour: no request
    assert srv.calls.count(("GET", "/cli/version")) == 1
    assert await ask(BASE, 1000.0 + 3601) == sha(NEW)                 # after it: asks again
    assert srv.calls.count(("GET", "/cli/version")) == 2
    assert await ask("http://other:2", 1000.0 + 3602) == sha(NEW)     # another server: asks
    assert srv.calls.count(("GET", "/cli/version")) == 3
    assert (cfg / "update-check.json").exists()


async def test_an_older_server_without_the_endpoint_is_asked_once_an_hour_too(cfg):
    srv = UpdSrv()
    srv.version_status = 404
    ask = lambda now: jav3._server_client_sha(BASE, srv.transport(), now)    # noqa: E731
    assert await ask(5.0) is None and await ask(6.0) is None
    assert srv.calls.count(("GET", "/cli/version")) == 1


async def test_an_unreachable_server_is_not_stamped_so_the_next_start_asks_again(cfg):
    srv = UpdSrv()
    srv.down = True
    with pytest.raises(httpx.ConnectError):
        await jav3._server_client_sha(BASE, srv.transport(), 5.0)
    assert not (cfg / "update-check.json").exists()
    srv.down = False
    assert await jav3._server_client_sha(BASE, srv.transport(), 6.0) == sha(NEW)


async def test_a_garbled_stamp_or_answer_is_ignored(cfg):
    cfg.mkdir(parents=True)
    (cfg / "update-check.json").write_text("{not json")
    srv = UpdSrv()
    assert await jav3._server_client_sha(BASE, srv.transport(), 5.0) == sha(NEW)
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"sha256": "nope"}))
    (cfg / "update-check.json").unlink()
    assert await jav3._server_client_sha(BASE, bad, 5.0) is None


# --- the TUI: the quiet notice -------------------------------------------------------------

async def boot(srv, **kw):
    return jav3.build_tui(BASE, "jvd_x", transport=srv.transport(), **kw)


async def test_a_different_client_on_the_server_is_one_quiet_notice(installed):
    srv = UpdSrv()
    app = await boot(srv)
    toasts = []
    async with app.run_test(size=(80, 24)):        # narrow: no sidebar, so a toast would show
        app.notify = lambda *a, **k: toasts.append(a)
        assert await wait_for(lambda: any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices))
        assert [n for n in app.notices if n[2] == jav3.UPDATE_NOTICE][0][1] == "info"
        assert app.unread >= 1 and toasts == []             # counted in the status row, no toast
    assert jav3.UPDATE_NOTICE == "a newer jav3 is on the server: /update"
    assert installed.read_text() == OLD                     # noticing changes nothing


async def test_the_same_client_is_no_notice(installed):
    installed.write_text(NEW)
    srv = UpdSrv()
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        assert await wait_for(lambda: ("GET", "/cli/version") in srv.calls)
        await pilot.pause(0.3)
        assert not any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices)


async def test_a_second_start_within_the_hour_notices_from_the_stamp_without_asking(installed):
    srv = UpdSrv()
    for run in (1, 2):
        app = await boot(srv)
        async with app.run_test(size=(120, 40)):
            assert await wait_for(lambda: any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices))
        assert srv.calls.count(("GET", "/cli/version")) == 1, run


async def test_the_check_never_holds_up_the_first_frame(installed):
    srv = UpdSrv()
    srv.gate = asyncio.Event()                  # the server sits on /cli/version
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        # the login check and the pickers' data finish while the version answer is pending
        assert await wait_for(lambda: app.logged_in and app.options)
        await pilot.pause(0.2)
        assert not any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices)
        srv.gate.set()
        assert await wait_for(lambda: any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices))


async def test_nothing_to_say_when_the_server_cannot_be_asked_or_is_older(installed):
    for how in ("down", "old"):
        srv = UpdSrv()
        srv.down = how == "down"
        srv.version_status = 404 if how == "old" else 200
        app = await boot(srv)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            assert not any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices), how


async def test_a_checkout_is_never_asked_or_nagged(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "clients" / "jav3cli").mkdir(parents=True)
    path = repo / "clients" / "jav3cli" / "jav3"
    path.write_text(OLD)
    monkeypatch.setattr(jav3, "_client_file", lambda: path)
    srv = UpdSrv()
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        assert ("GET", "/cli/version") not in srv.calls
        assert not any(t == jav3.UPDATE_NOTICE for _, _, t in app.notices)


# --- /update ---------------------------------------------------------------------------------

async def test_update_replaces_the_file_keeps_a_backup_and_leaves_the_rest(installed):
    srv = UpdSrv()
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/update")
        assert await wait_for(lambda: any("Restart jav3 to use it" in n for n in notes(app)))
        day = time.strftime("%Y-%m-%d")
        assert any(f"jav3.bak-{day}" in n for n in notes(app))     # the note names the backup
    assert installed.read_text() == NEW
    assert (installed.parent / f"jav3.bak-{day}").read_text() == OLD
    # the temp file is gone; the launcher and the venv are as they were
    assert sorted(p.name for p in installed.parent.iterdir()) == [
        "jav3", f"jav3.bak-{day}", "launcher", "venv"]
    assert (installed.parent / "launcher").read_text() == "#!/bin/sh\nexec python jav3\n"
    assert (installed.parent / "venv" / "bin" / "python").read_text() == "venv python"


async def test_update_refuses_a_download_that_is_not_python(installed):
    srv = UpdSrv(served="<html><body>sign in to continue</body></html>")
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/update")
        assert await wait_for(lambda: any("not valid Python" in n for n in notes(app)))
    untouched(installed)


async def test_update_in_a_checkout_says_to_git_pull(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "clients" / "jav3cli").mkdir(parents=True)
    path = repo / "clients" / "jav3cli" / "jav3"
    path.write_text(OLD)
    monkeypatch.setattr(jav3, "_client_file", lambda: path)
    srv = UpdSrv()
    app = await boot(srv)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await send(pilot, app, "/update")
        assert await wait_for(lambda: any("you are running from a checkout" in n for n in notes(app)))
        assert any("git pull instead" in n for n in notes(app))
    assert path.read_text() == OLD and sorted(os.listdir(path.parent)) == ["jav3"]
    assert ("GET", "/cli/jav3") not in srv.calls            # refused before downloading anything


async def test_update_while_a_turn_runs_is_allowed(installed):
    # swapping the file under a running TUI is safe: it holds its code in memory
    srv = UpdSrv()
    app = await boot(srv)
    assert app.commands["update"].busy_ok


def test_update_not_writable_names_the_installer(installed):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    installed.parent.chmod(0o500)
    try:
        with pytest.raises(jav3.CliError) as e:
            jav3.update_client(BASE, installed, UpdSrv().transport())
    finally:
        installed.parent.chmod(0o700)
    assert "not writable" in str(e.value) and f"curl -fsSL {BASE}/cli/install.sh | sh" in str(e.value)
    untouched(installed)


def test_update_with_libraries_too_old_prints_the_install_line_and_changes_nothing(
        installed, monkeypatch):
    from importlib import metadata
    monkeypatch.setattr(metadata, "version", lambda n: "7.0.0")
    srv = UpdSrv(requires=["textual>=8,<9"])
    with pytest.raises(jav3.CliError) as e:
        jav3.update_client(BASE, installed, srv.transport())
    msg = str(e.value)
    assert "textual 7.0.0 (needs >=8,<9)" in msg and f"curl -fsSL {BASE}/cli/install.sh | sh" in msg
    untouched(installed)
    # the same file with libraries that fit goes through
    monkeypatch.setattr(metadata, "version", lambda n: "8.0.0")
    assert "Restart jav3 to use it" in jav3.update_client(BASE, installed, srv.transport())
    assert installed.read_text() == NEW


def test_update_when_already_current_changes_nothing(installed):
    installed.write_text(NEW)
    msg = jav3.update_client(BASE, installed, UpdSrv().transport())
    assert "up to date" in msg
    assert sorted(p.name for p in installed.parent.iterdir()) == ["jav3", "launcher", "venv"]


def test_a_second_update_the_same_day_keeps_both_backups(installed):
    jav3.update_client(BASE, installed, UpdSrv().transport(), today="2026-10-01")
    jav3.update_client(BASE, installed, UpdSrv(served=NEW + "# again\n").transport(),
                       today="2026-10-01")
    assert (installed.parent / "jav3.bak-2026-10-01").read_text() == OLD
    assert (installed.parent / "jav3.bak-2026-10-01-2").read_text() == NEW
    assert installed.read_text() == NEW + "# again\n"


def test_update_server_errors_and_redirects_leave_the_file_alone(installed):
    for resp in (httpx.Response(503, text="down"),
                 httpx.Response(302, headers={"location": "https://login.example/x"})):
        with pytest.raises(jav3.CliError):
            jav3.update_client(BASE, installed, httpx.MockTransport(lambda r, resp=resp: resp))
        untouched(installed)

    def boom(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(jav3.CliError, match="could not reach"):
        jav3.update_client(BASE, installed, httpx.MockTransport(boom))
    untouched(installed)


def test_a_binary_download_is_refused(installed):
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=b"\xff\xfe\x00bad"))
    with pytest.raises(jav3.CliError, match="not valid Python"):
        jav3.update_client(BASE, installed, t)
    untouched(installed)


# --- jav3 update ------------------------------------------------------------------------------

def test_the_update_subcommand_is_parsed():
    args = jav3.parse_args(["update"])
    assert args.cmd == "update" and args.prompt == []
    assert jav3.parse_args(["--server", "h:1", "update"]).server == "h:1"
    assert "update" in jav3.build_parser().format_usage()


def test_jav3_update_uses_the_saved_server_and_prints_the_result(installed):
    jav3.save_credentials("h:1", "jvd_x")
    srv = UpdSrv()
    out = io.StringIO()
    assert jav3.cmd_update(argparse.Namespace(server=None), out, transport=srv.transport()) == 0
    assert "Restart jav3 to use it" in out.getvalue()
    assert installed.read_text() == NEW
    assert (installed.parent / f"jav3.bak-{time.strftime('%Y-%m-%d')}").read_text() == OLD


def test_jav3_update_without_a_server_or_login_says_so(installed):
    with pytest.raises(jav3.CliError, match="no server"):
        jav3.cmd_update(argparse.Namespace(server=None), io.StringIO())
    # --server works without a login: the file is public like the installer
    out = io.StringIO()
    jav3.cmd_update(argparse.Namespace(server="h:1"), out, transport=UpdSrv().transport())
    assert installed.read_text() == NEW


def test_main_routes_update_and_prints_refusals_as_one_line(installed, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(jav3, "update_client", lambda base, **kw: asked.append(base) or "ok")
    assert jav3.main(["--server", "h:1", "update"]) == 0 and asked == ["http://h:1"]

    def refuse(base, **kw):
        raise jav3.CliError("you are running from a checkout; git pull instead")
    monkeypatch.setattr(jav3, "update_client", refuse)
    assert jav3.main(["--server", "h:1", "update"]) == 1
    assert capsys.readouterr().err.strip() == (
        "jav3: you are running from a checkout; git pull instead")
