"""Dashboard tool: single-file HTML written under dashboards/, path-validated."""
import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds


@pytest.fixture
async def client(tmp_env):
    await init_db()
    ensure_memory_seeds()
    db = await get_db()
    try:
        await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         ("operator", hash_password("hunter2")))
        await db.commit()
    finally:
        await db.close()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login",
                     json={"username": "operator", "password": "hunter2"})
        await c.post("/api/projects", json={"name": "Demo", "summary": "demo"})
        await c.post("/api/projects/demo/load")
        yield c


def test_dashboard_tool_registered(tmp_env):
    assert "dashboard" in {e["name"] for e in registry.compile_registry()}


async def test_dashboard_written_under_dashboards(client):
    out = await registry.dispatch(
        "dashboard", {"path": "metrics.html", "html": "<h1>hi</h1>"})
    assert "dashboard written at dashboards/metrics.html" in out
    live = settings.projects_dir / "demo" / "dashboards" / "metrics.html"
    assert live.read_text() == "<h1>hi</h1>"


async def test_dashboard_keeps_existing_prefix(client):
    out = await registry.dispatch(
        "dashboard", {"path": "dashboards/x.html", "html": "<p>x</p>"})
    assert "dashboard written at dashboards/x.html" in out
    assert "dashboards/dashboards" not in out


async def test_dashboard_rejects_bad_paths(client):
    for bad in ("plain.txt", "../evil.html", "/abs.html", "a/../../up.html", ""):
        out = await registry.dispatch("dashboard", {"path": bad, "html": "x"})
        assert out.startswith("error"), f"path {bad!r} should be rejected"
    # nothing got written by any of the rejects
    assert not (settings.projects_dir / "demo" / "dashboards").exists()
