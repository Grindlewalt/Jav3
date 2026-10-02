"""run_code: the in-guest execution tool. The handler runs verbatim in the
guest; here we simulate guest conditions (in_guest flag + task-local slug) and
verify execution, capture, caps, and the host-side guards."""
import asyncio
import os
import time

from backend.agent.tools import registry, toolctx
from backend.config import settings
from backend.db import init_db


def _guest(monkeypatch, tmp_path, slug="proj"):
    """Impersonate the guest: flag on, workspace copy under tmp."""
    monkeypatch.setattr(settings, "in_guest", True)
    (tmp_path / slug).mkdir(parents=True, exist_ok=True)

    async def fake_slug():
        return slug
    monkeypatch.setattr(toolctx, "active_slug", fake_slug)


async def test_offered(tmp_env):
    """Every turn's loop runs in the guest (M4e removed the host path), so
    run_code is simply part of the registry — no flag gates it any more. The
    host handler still self-guards on in_guest; see test_host_dispatch_refuses."""
    await init_db()
    registry.compile_registry()
    names = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "run_code" in names


async def test_host_dispatch_refuses(tmp_env):
    """On the host (no in_guest flag) the handler must refuse — code execution
    exists nowhere outside the guest."""
    await init_db()
    out = await registry.dispatch("run_code", {"code": "print('nope')"})
    assert out.startswith("error:") and "guest" in out
    assert "nope" not in out


async def test_runs_python_and_keeps_artifacts(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    (tmp_path / "proj" / "input.txt").write_text("21")

    out = await registry.dispatch("run_code", {"code": (
        "n = int(open('input.txt').read())\n"
        "open('answer.txt', 'w').write(str(n * 2))\n"
        "print('doubled to', n * 2)")})
    assert "exit 0" in out
    assert "doubled to 42" in out
    # the created file was captured through the writes chokepoint
    assert (tmp_path / "proj" / "answer.txt").read_text() == "42"
    assert "kept 1 changed file(s): answer.txt" in out
    # the unchanged input file was NOT captured
    assert "input.txt" not in out.split("kept 1")[1]


async def test_runs_shell_command(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": "echo hello-$((6*7))"})
    assert "exit 0" in out and "hello-42" in out


async def test_nonzero_exit_and_stderr_surface(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"code": "import sys; sys.exit(3)"})
    assert "exit 3" in out
    out = await registry.dispatch("run_code", {"code": "raise ValueError('boom')"})
    assert "exit 1" in out and "boom" in out and "stderr" in out


async def test_timeout_kills_process_group(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        "code": "import time; print('started', flush=True); time.sleep(30)",
        "timeout_seconds": 1}), 15)
    assert "KILLED after 1s timeout" in out
    assert "started" in out          # pre-kill output survives


async def test_arg_validation(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    both = await registry.dispatch("run_code", {"code": "1", "command": "true"})
    neither = await registry.dispatch("run_code", {})
    assert both.startswith("error:") and neither.startswith("error:")


async def test_no_project_scratch_mode(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(settings, "in_guest", True)

    async def no_slug():
        return None
    monkeypatch.setattr(toolctx, "active_slug", no_slug)
    out = await registry.dispatch("run_code", {"code": "print(2**10)"})
    assert "1024" in out
    assert "not kept" in out         # explicit: no artifact persistence


async def test_output_truncated_head_and_tail(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {
        "code": "print('A'*20000 + 'ENDMARK')"})
    assert "truncated" in out
    assert "ENDMARK" in out          # the tail survives truncation


async def test_protected_paths_never_captured(tmp_env, monkeypatch, tmp_path):
    """A run that writes into .git must not smuggle it through the capture."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command":
        "mkdir -p .git && echo x > .git/hook && echo ok > fine.txt"})
    assert "exit 0" in out
    assert "kept 1 changed file(s): fine.txt" in out
    assert ".git" not in out.split("kept 1")[1].splitlines()[0]


async def test_sees_this_turns_pending_writes(tmp_env, monkeypatch, tmp_path):
    """write_file buffers into .staging/; run_code must see those files
    (2026-09-27: `node --test tests/x.test.mjs` found nothing) without
    re-capturing them as its own artifacts."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    ws = tmp_path / "proj"
    (ws / "old.txt").write_text("stale")
    (ws / ".staging" / "tests").mkdir(parents=True)
    (ws / ".staging" / "tests" / "new.txt").write_text("fresh")
    (ws / ".staging" / "old.txt").write_text("edited")

    out = await registry.dispatch("run_code", {"command": "cat tests/new.txt old.txt"})
    assert "freshedited" in out
    assert "kept" not in out          # synced files are not the run's artifacts


async def test_cache_trees_are_not_reported_as_kept(tmp_env, monkeypatch, tmp_path):
    """BUILD-04: the host drops __pycache__/.pytest_cache at turn end, so run_code
    must not say it kept them (agents burned calls cleaning up); no .pyc at all."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": (
        "mkdir -p pkg/__pycache__ .pytest_cache && echo x > pkg/__pycache__/a.pyc "
        "&& echo x > .pytest_cache/CACHEDIR.TAG && echo real > real.txt "
        "&& echo import-dont-write-bytecode: $PYTHONDONTWRITEBYTECODE")})
    assert "kept 1 changed file(s): real.txt" in out
    assert "import-dont-write-bytecode: 1" in out


async def test_background_process_holding_pipes_does_not_hang(tmp_env, monkeypatch, tmp_path):
    """PLANS-01: `cmd &` with the output not redirected leaves the background
    process holding the pipes. The shell has exited, so the call must return
    within seconds (not when the server dies) and say what is still running."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    t0 = time.monotonic()
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        "command": "sleep 6 & echo started", "timeout_seconds": 3}), 12)
    assert time.monotonic() - t0 < 8
    assert "exit 0" in out and "started" in out
    assert "still running" in out and "Redirect" in out


async def test_timeout_kill_reaches_a_reaped_shell_group(tmp_env, monkeypatch, tmp_path):
    """The timeout kill must not need the shell's pid to still be alive
    (os.getpgid on a reaped pid raised ProcessLookupError and skipped the
    kill): a shell that exits while its child keeps running is still killed."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    t0 = time.monotonic()
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        "command": "(sleep 6; echo late) & sleep 0.2; echo shell-done; exit 0",
        "timeout_seconds": 1}), 12)
    assert time.monotonic() - t0 < 8
    assert "shell-done" in out and "late" not in out


async def test_pkill_f_of_the_commands_own_text_does_not_kill_the_shell(
        tmp_env, monkeypatch, tmp_path):
    """PLANS-10: with `sh -c <command>` the whole command sits in the shell's
    cmdline, so `pkill -f <text from it>` killed the shell itself (exit -15, ten
    times across three plan runs). The command now runs from a script file."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        "command": "sleep 31.7 > /dev/null 2>&1 & pkill -f 'sleep 31.7'; echo survived"}), 12)
    assert "exit 0" in out and "survived" in out
    # procps' pkill -f (Linux) matches the shell's own cmdline; BSD's spares its
    # parent, so also check directly that the command text is not in it
    out = await registry.dispatch("run_code", {
        "command": "echo cmdline-is: $(ps -o args= -p $$) MARKER-4471"})
    assert "cmdline-is:" in out and out.count("MARKER-4471") == 1


async def test_network_hint_does_not_need_a_failing_exit_code(tmp_env, monkeypatch, tmp_path):
    """PLANS-06 / BUILD-05: `pip install ... | tail`, `cmd; echo done` and a
    curl that only prints its tunnel error exit 0, and the model got no hint."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setenv("JARVIS_EGRESS_PROXY", "http://10.201.0.1:3128")
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": (
        "echo 'curl: (56) CONNECT tunnel failed, response 403 for https://pypi.org/x'; true")})
    assert "exit 0" in out and "[network blocked: pypi.org" in out
    # an exit-0 run that merely mentions a weak marker is left alone
    out = await registry.dispatch("run_code", {"command": "echo 'connection refused test passed'"})
    assert "network blocked" not in out
    # and a failing run keeps the wider set
    out = await registry.dispatch("run_code", {"command": "echo 'connection refused' >&2; exit 1"})
    assert "network blocked" in out


# --- benchmark game 2026-10-01: SIGXFSZ on package installs, lost output on a kill ---

def test_file_size_limit_fits_a_package_install():
    """64 MiB killed `apt-get install chromium` (its .deb is bigger): the per-file
    limit must clear any package."""
    from tools.run_code import handler
    assert handler.FSIZE_LIMIT >= 1024 * 1024 * 1024


def test_limits_sets_the_file_size_limit_to_the_constant(monkeypatch):
    import resource
    from backend import memguard
    from tools.run_code import handler
    seen = {}
    monkeypatch.setattr(resource, "setrlimit", lambda lim, v: seen.__setitem__(lim, v))
    monkeypatch.setattr(memguard, "confine", lambda: None)
    monkeypatch.setattr(os, "setsid", lambda: None)
    handler._limits(10)
    assert seen[resource.RLIMIT_FSIZE] == (handler.FSIZE_LIMIT, handler.FSIZE_LIMIT)


async def test_run_that_hits_the_file_size_limit_names_it(tmp_env, monkeypatch, tmp_path):
    """A write past RLIMIT_FSIZE is SIGXFSZ: the result says "file-size limit (N MB)"
    instead of leaving the bare signal to be guessed at."""
    from tools.run_code import handler
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    monkeypatch.setattr(handler, "FSIZE_LIMIT", 1024 * 1024)       # 1 MB for the test
    _guest(monkeypatch, tmp_path)
    big = str(tmp_path / "rc-big.bin")
    out = await handler.run(code=f"open({big!r}, 'wb').write(b'x' * (4 * 1024 * 1024))")
    assert "file-size limit (1 MB)" in out, out


async def test_normal_run_has_no_file_size_or_timeout_note(tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command": "echo fine; echo 'file too large' >&2"})
    assert "file-size limit" not in out and "timed out" not in out


async def test_timeout_kill_says_the_limit_and_that_piped_output_is_lost(
        tmp_env, monkeypatch, tmp_path):
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        "command": "(echo before; sleep 30) | tail -25", "timeout_seconds": 1}), 15)
    assert "KILLED after 1s timeout" in out
    assert "this call's limit is 1s" in out
    assert "piped into tail/head/grep is lost" in out and "write long output to a file" in out
    assert "your `timeout" not in out          # the command asked for no timeout of its own


async def test_inner_timeout_longer_than_the_call_limit_is_named(
        tmp_env, monkeypatch, tmp_path):
    """The benchmark-game call wrapped `timeout 280` but passed no timeout_seconds
    and died at the 60 s default (here: a 1 s call limit, to keep the test short)."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await asyncio.wait_for(registry.dispatch("run_code", {
        # a function stands in for coreutils timeout, which a Mac does not have
        "command": 'timeout() { shift; "$@"; }; timeout 280 sleep 30 | tail -25',
        "timeout_seconds": 1}), 15)
    assert "KILLED after 1s timeout" in out
    assert "your `timeout 280` is longer than this call's timeout_seconds (1)" in out
    assert "pass timeout_seconds: 290" in out


def test_inner_timeout_parse():
    from tools.run_code.handler import _inner_timeout
    assert _inner_timeout("timeout 280 make") == 280
    assert _inner_timeout("timeout -s KILL 5m make") == 300
    assert _inner_timeout("timeout -k 10 90 make; timeout 20 x") == 90
    assert _inner_timeout("timeout --foreground 45 cmd") == 45
    assert _inner_timeout("curl --timeout 5 url") is None       # a flag, not the command
    assert _inner_timeout("echo timeout_seconds 99") is None
    assert _inner_timeout("make") is None
