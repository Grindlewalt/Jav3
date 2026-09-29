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
