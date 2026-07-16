"""run_code: the in-guest execution tool. The handler runs verbatim in the
guest; here we simulate guest conditions (in_guest flag + task-local slug) and
verify execution, capture, caps, and the host-side guards."""
import asyncio

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


async def test_not_offered_without_guest_loop(tmp_env):
    await init_db()
    registry.compile_registry()
    assert settings.use_guest_loop is False
    names = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "run_code" not in names


async def test_offered_with_guest_loop(tmp_env, monkeypatch):
    await init_db()
    registry.compile_registry()
    monkeypatch.setattr(settings, "use_guest_loop", True)
    names = {s["function"]["name"] for s in registry.openai_tool_specs()}
    assert "run_code" in names


async def test_host_dispatch_refuses(tmp_env):
    """On the host (no in_guest flag) the handler must refuse — code execution
    exists nowhere outside the guest."""
    await init_db()
    out = await registry.dispatch("run_code", {"code": "print('nope')"})
    assert out.startswith("error:") and "guest" in out
    assert "nope" not in out


async def test_runs_python_and_stages_artifacts(tmp_env, monkeypatch, tmp_path):
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
    # the created file was captured into staging, canonical untouched
    assert (tmp_path / "proj" / ".staging" / "answer.txt").read_text() == "42"
    assert not (tmp_path / "proj" / "answer.txt").exists() or \
        (tmp_path / "proj" / "answer.txt").read_text() == "42"  # guest copy may hold it
    assert "staged 1 changed file(s)" in out
    # the unchanged input file was NOT staged
    assert not (tmp_path / "proj" / ".staging" / "input.txt").exists()


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


async def test_protected_paths_never_staged(tmp_env, monkeypatch, tmp_path):
    """A run that writes into .git must not smuggle it through staging."""
    await init_db()
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    _guest(monkeypatch, tmp_path)
    out = await registry.dispatch("run_code", {"command":
        "mkdir -p .git && echo x > .git/hook && echo ok > fine.txt"})
    assert "exit 0" in out
    assert (tmp_path / "proj" / ".staging" / "fine.txt").exists()
    assert not (tmp_path / "proj" / ".staging" / ".git").exists()
