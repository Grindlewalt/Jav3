"""Backlog B8 (tool wording and offering): RUNS-09 load_project's note, RUNS-05
tools that can only fail, the run_code 'queued' claim."""
import pytest

from backend.agent import budget as budget_mod
from backend.agent.tools import registry
from backend.vm import broker
from tests.test_tools import client  # noqa: F401


@pytest.fixture
def guest_turn_env():
    """A brokered guest turn: what load_project sees when the loop runs in the
    guest (an envelope registered for the op). The op id is set in the test's
    own context (a sync fixture's context is not the test's)."""
    def make(active_project):
        env = broker.TurnEnvelope(op_id="op-b8", conversation_id=None,
                                  active_project=active_project)
        broker.register_turn(env)
        budget_mod.active_op_id.set("op-b8")
        return env

    yield make
    broker.release_turn("op-b8")


# --- RUNS-09 ------------------------------------------------------------------

async def test_load_project_with_no_previous_project_says_file_tools_wait(
        client, guest_turn_env):
    """conv 574: 'file tools finish this turn on the previous project's sandbox
    workspace' with no previous project, then list_files failed."""
    env = guest_turn_env(None)                    # the turn started with no project
    out = await registry.dispatch("load_project", {"slug": "demo"})
    assert "loaded project 'demo'" in out
    assert "previous project" not in out          # there was none
    assert "no workspace" in out and "next turn" in out
    assert env.active_project == "demo"           # brokered children still resolve it


async def test_load_project_from_another_project_keeps_the_previous_note(
        client, guest_turn_env):
    await client.post("/api/projects", json={"name": "Second", "summary": "two"})
    guest_turn_env("demo")
    out = await registry.dispatch("load_project", {"slug": "second"})
    assert "previous project's sandbox workspace" in out
    assert "next turn" in out


async def test_reloading_the_same_project_needs_no_warning(client, guest_turn_env):
    guest_turn_env("demo")
    out = await registry.dispatch("load_project", {"slug": "demo"})
    assert "loaded project 'demo'" in out
    assert "(note:" not in out


# --- run_code 'queued' honesty ---------------------------------------------------

async def _failed_net_call(monkeypatch, tmp_path, command, proxy="http://10.201.0.1:3128"):
    from backend.agent.tools import toolctx
    from backend.config import settings
    monkeypatch.setattr(settings, "in_guest", True)
    monkeypatch.setattr(settings, "projects_dir", tmp_path)
    (tmp_path / "proj").mkdir(parents=True, exist_ok=True)

    async def fake_slug():
        return "proj"
    monkeypatch.setattr(toolctx, "active_slug", fake_slug)
    if proxy:
        monkeypatch.setenv("JARVIS_EGRESS_PROXY", proxy)
    else:
        monkeypatch.delenv("JARVIS_EGRESS_PROXY", raising=False)
    return await registry.dispatch("run_code", {"command": command})


@pytest.mark.parametrize("target", ["http://10.201.0.1:3000/repo.git",   # the host (Gitea)
                                    "http://169.254.169.254/latest/meta-data",
                                    "http://192.168.1.10:8000/",
                                    "http://localhost:9/"])
async def test_run_code_never_says_queued_for_a_refused_host(tmp_env, monkeypatch, tmp_path,
                                                             target):
    """The proxy refuses the host, loopback, LAN and metadata addresses outright
    and never queues them; the old note told the model to ask the operator to
    approve something no approval can open."""
    out = await _failed_net_call(
        monkeypatch, tmp_path,
        f"echo 'curl: (7) Failed to connect to {target}: Connection refused' >&2; exit 7")
    assert "QUEUED" not in out and "queued" not in out.lower().replace("does not queue", "")
    assert "not queue" in out.lower() and "nothing for the operator to approve" in out


async def test_run_code_public_host_note_does_not_promise_a_queue(tmp_env, monkeypatch,
                                                                  tmp_path):
    out = await _failed_net_call(
        monkeypatch, tmp_path,
        "echo 'Could not resolve host: registry.example.org' >&2; "
        "echo 'fetching https://registry.example.org/x' ; exit 6")
    assert "registry.example.org" in out
    assert "QUEUED" not in out and "are now queued" not in out
    assert "Network tab" in out


async def test_run_code_egress_off_note_unchanged(tmp_env, monkeypatch, tmp_path):
    out = await _failed_net_call(
        monkeypatch, tmp_path, "echo 'Network is unreachable' >&2; exit 1", proxy=None)
    assert "monitored egress is OFF" in out
