"""Secrets vault: the agent uses {{secret:NAME}} placeholders; the host
substitutes at VM-execution time, scrubs echoes from outputs, and flags staged
files carrying a value. The API never returns a value."""
import httpx
import pytest

from backend import secrets
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import assemble_system_prompt, ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("operator", hash_password("hunter2")),
        )
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        yield c


def test_substitute_and_scrub(tmp_env):
    secrets.save({"TBA_KEY": "sk-real-value-123"})
    out = secrets.substitute('curl -H "X-Key: {{secret:TBA_KEY}}" api.example')
    assert "sk-real-value-123" in out and "{{secret:" not in out
    # lowercase placeholder names resolve too
    assert "sk-real-value-123" in secrets.substitute("{{secret:tba_key}}")
    # scrub puts the placeholder back
    scrubbed = secrets.scrub("the key is sk-real-value-123, don't tell")
    assert "sk-real-value-123" not in scrubbed
    assert "{{secret:TBA_KEY}}" in scrubbed
    # unknown name: fix-shaped KeyError listing what exists
    with pytest.raises(KeyError) as e:
        secrets.substitute("{{secret:NOPE}}")
    assert "TBA_KEY" in str(e.value)


def test_substitute_noop_without_placeholders(tmp_env):
    assert secrets.substitute("plain command") == "plain command"
    assert secrets.substitute(None) is None
    assert secrets.scrub(None) is None


async def test_vmexec_substitutes_and_scrubs(tmp_env, monkeypatch):
    """run_in_project (shared by run_command/run_code/run_gated) is the
    chokepoint: real value goes into the VM, placeholder comes back out."""
    from backend.agent.tools import vmexec

    secrets.save({"API_KEY": "sekrit-value-42"})
    proj = settings.projects_dir / "demo"
    proj.mkdir(parents=True)
    (proj / "project.md").write_text("# demo")
    seen = {}

    async def fake_push(merged, slug):
        return {"bytes": 0}

    async def fake_run(command, timeout=None, cwd=None, input=None):
        seen["command"], seen["input"] = command, input
        return {"exit_status": 0, "timed_out": False,
                "stdout": "your key sekrit-value-42 works", "stderr": ""}

    async def fake_pull(slug, dest):
        (dest / slug).mkdir(parents=True, exist_ok=True)
        return {"bytes": 0}

    monkeypatch.setattr(vmexec.vm, "push", fake_push)
    monkeypatch.setattr(vmexec.vm, "run", fake_run)
    monkeypatch.setattr(vmexec.vm, "pull", fake_pull)

    r = await vmexec.run_in_project(
        "demo", 'curl -H "K: {{secret:API_KEY}}"', input="x = '{{secret:API_KEY}}'")
    assert seen["command"] == 'curl -H "K: sekrit-value-42"'
    assert seen["input"] == "x = 'sekrit-value-42'"
    # echoed value scrubbed back to the placeholder before the model sees it
    assert "sekrit-value-42" not in r["stdout"]
    assert "{{secret:API_KEY}}" in r["stdout"]


async def test_vmexec_unknown_secret_is_vmerror(tmp_env, monkeypatch):
    from backend.agent.tools import vm, vmexec
    secrets.save({"REAL": "abcdefgh"})
    with pytest.raises(vm.VMError) as e:
        await vmexec.run_in_project("demo", "echo {{secret:FAKE}}")
    assert "unknown secret 'FAKE'" in str(e.value) and "REAL" in str(e.value)


async def test_secrets_api_never_returns_values(client):
    r = await client.put("/api/secrets/tba_key", json={"value": "sk-live-999888"})
    assert r.status_code == 200 and r.json()["name"] == "TBA_KEY"
    r = await client.get("/api/secrets")
    body = r.text
    assert "sk-live-999888" not in body
    assert r.json()["secrets"] == [{"name": "TBA_KEY", "last4": "9888"}]
    r = await client.put("/api/secrets/bad name!", json={"value": "x"})
    assert r.status_code == 400
    r = await client.delete("/api/secrets/TBA_KEY")
    assert r.status_code == 200
    assert (await client.get("/api/secrets")).json()["secrets"] == []


async def test_secret_names_ride_context(tmp_env):
    ensure_memory_seeds()
    secrets.save({"STATBOTICS_KEY": "v" * 12})
    prompt = await assemble_system_prompt(None, active=None)
    assert "STATBOTICS_KEY" in prompt
    assert "{{secret:NAME}}" in prompt
    assert "v" * 12 not in prompt          # never the value
