import pytest

from backend import gui
from backend.config import settings


@pytest.fixture(autouse=True)
def clean_player():
    """Reset the in-page player between tests.

    `gui._player` is a process global on purpose — it describes a browser tab
    that exists right now, exactly like TARMAC's own playerState. That makes it
    leak across test files: a test that left a track loaded made a later test's
    `auto` routing pick the in-page player and never reach the music server.
    Harmless in production, where the 30s staleness check covers it, but it
    makes test outcomes depend on file order.
    """
    gui.player_report({"track": None, "paused": True, "position": 0,
                       "duration": None, "queue": 0, "volume": 100,
                       "started": False, "error": ""})
    yield


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Point all durable-state paths at a temp dir so tests never touch real data."""
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "jarvis.db")
    monkeypatch.setattr(settings, "memory_dir", tmp_path / "memory")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")
    monkeypatch.setattr(settings, "skills_dir", tmp_path / "skills")
    monkeypatch.setattr(settings, "agents_dir", tmp_path / "agents")
    monkeypatch.setattr(settings, "secrets_path", tmp_path / "secrets.json")
    return tmp_path
