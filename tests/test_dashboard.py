"""Dashboard tool: single-file HTML staged under dashboards/, path-validated."""
import httpx
import pytest

from backend.agent.tools import registry
from backend.auth import hash_password
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.memory import ensure_memory_seeds
from backend.staging import list_staged


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


async def test_dashboard_is_staged_under_dashboards(client):
    out = await registry.dispatch(
        "dashboard", {"path": "metrics.html", "html": "<h1>hi</h1>"})
    assert "staged dashboard at dashboards/metrics.html" in out
    staged = (settings.projects_dir / "demo" / ".staging"
              / "dashboards" / "metrics.html")
    assert staged.read_text() == "<h1>hi</h1>"
    # canonical stays untouched until approval
    assert not (settings.projects_dir / "demo" / "dashboards" / "metrics.html").exists()
    assert [e["path"] for e in list_staged("demo")] == ["dashboards/metrics.html"]


async def test_dashboard_keeps_existing_prefix(client):
    out = await registry.dispatch(
        "dashboard", {"path": "dashboards/x.html", "html": "<p>x</p>"})
    assert "staged dashboard at dashboards/x.html" in out
    assert "dashboards/dashboards" not in out


async def test_dashboard_rejects_bad_paths(client):
    for bad in ("plain.txt", "../evil.html", "/abs.html", "a/../../up.html", ""):
        out = await registry.dispatch("dashboard", {"path": bad, "html": "x"})
        assert out.startswith("error"), f"path {bad!r} should be rejected"
    # nothing got staged by any of the rejects
    assert list_staged("demo") == []
