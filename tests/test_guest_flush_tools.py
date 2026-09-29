"""Host-side tools that look for project files see what this turn already wrote.

A turn that runs in a guest VM keeps write_file/edit_file in the VM until its
final message, so a tool that runs on the HOST and looks for the file said
"nothing matching 'README.md' ... These are in this project: project.md" right
after write_file wrote README.md (harness faults #7 and #10, 2026-09-29). The
git tools got gitgate.flush_guest_writes the day before (test_gitea_overnight);
these are the other tools that read the project directory on the host:
workspace_panel, play_music / play_movie, orchestrate and service_request."""
import contextlib

import pytest

from backend.agent import budget
from backend.agent.tools import registry
from backend.config import settings
from backend.vm import broker, guest_turn


@pytest.fixture
def demo(tmp_env):
    d = settings.projects_dir / "demo"
    d.mkdir(parents=True)
    (d / "project.md").write_text("# demo\n")
    return d


@contextlib.contextmanager
def in_guest_turn(monkeypatch, staged: dict[str, str]):
    """The next tool call is brokered from a guest turn whose write buffer holds
    `staged`: pull_writes lands it host-side, like apply_guest_writes."""
    calls: list[str] = []

    async def pull(slug):
        calls.append(slug)
        for rel, data in staged.items():
            p = settings.projects_dir / slug / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(data)

    monkeypatch.setattr(guest_turn, "pull_writes", pull)
    monkeypatch.setattr(broker, "get_turn", lambda op: object() if op == "op-flush" else None)
    tok = budget.active_op_id.set("op-flush")
    try:
        yield calls
    finally:
        budget.active_op_id.reset(tok)


async def _call(tool: str, **args) -> str:
    from backend import runtime
    runtime.active_project.set("demo")
    return await registry.dispatch(tool, args)


async def test_workspace_panel_opens_a_file_written_this_turn(demo, monkeypatch):
    # (before the fix: "error: nothing matching 'README.md' in 'demo'. These are in
    # this project: project.md")
    with in_guest_turn(monkeypatch, {"README.md": "# hi\n"}) as calls:
        out = await _call("workspace_panel", action="open_file", path="README.md")
    assert calls == ["demo"]
    assert out.startswith("added 'editor' panel on README.md"), out


async def test_workspace_panel_list_shows_this_turns_dashboard(demo, monkeypatch):
    with in_guest_turn(monkeypatch, {"dashboards/report.html": "<p>x</p>"}) as calls:
        out = await _call("workspace_panel", action="list")
    assert calls and "dashboards/report.html" in out, out


async def test_workspace_panel_layout_actions_do_not_pull(demo, monkeypatch):
    with in_guest_turn(monkeypatch, {}) as calls:
        await _call("workspace_panel", action="tile")
    assert calls == []


@pytest.mark.parametrize("tool", ["play_music", "play_movie"])
async def test_media_tools_find_a_file_written_this_turn(demo, monkeypatch, tool):
    with in_guest_turn(monkeypatch, {"out/clip.bin": "x"}) as calls:
        out = await _call(tool, source="out/clip.bin")
    assert calls == ["demo"]
    assert "no such file" not in out, out       # it got as far as looking for a tab


@pytest.mark.parametrize("tool", ["play_music", "play_movie"])
async def test_media_tools_do_not_pull_for_a_url(demo, monkeypatch, tool):
    with in_guest_turn(monkeypatch, {}) as calls:
        await _call(tool, source="https://example.com/a.mp3")
    assert calls == []


async def test_orchestrate_reads_files_written_this_turn(demo, monkeypatch):
    from backend import plan as plan_mod
    seen = []

    async def fake_plan(slug, dump, files, **kw):
        seen.append((demo / "spec.md").exists())
        raise ValueError("stop here")

    monkeypatch.setattr(plan_mod, "plan_from_dump", fake_plan)
    with in_guest_turn(monkeypatch, {"spec.md": "the spec"}) as calls:
        out = await _call("orchestrate", dump="build it", files=["spec.md"])
    assert calls == ["demo"] and seen == [True], (calls, seen, out)


async def test_service_request_snapshots_files_written_this_turn(demo, monkeypatch):
    from backend.vm import services
    seen = []

    async def fake_file_request(slug, args, **kw):
        seen.append((demo / "app.py").exists())
        raise services.ServiceError("stop here")

    monkeypatch.setattr(services, "file_request", fake_file_request)
    with in_guest_turn(monkeypatch, {"app.py": "print(1)"}) as calls:
        out = await _call("service_request", name="web", command=["python", "app.py"],
                          files=["app.py"], reason="serve it")
    assert calls == ["demo"] and seen == [True], (calls, seen, out)
