import pytest

from backend.config import settings


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
