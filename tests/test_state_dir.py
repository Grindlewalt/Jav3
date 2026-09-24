"""JARVIS_STATE_DIR: derivation, explicit overrides, legacy-layout detection,
and the migrate-state command on a throwaway tree."""
import logging
import sqlite3
import subprocess
from pathlib import Path

import pytest

from backend import config, statemigrate
from backend.config import Settings


def mk(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def _db(path: Path, projects_dir: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, path TEXT)")
    con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, summary TEXT)")
    con.execute("INSERT INTO projects (slug, path) VALUES ('p', ?)",
                (str((projects_dir or path.parent.parent / "projects") / "p"),))
    con.executemany("INSERT INTO conversations (summary) VALUES (?)",
                    [("a",), ("b",), ("c",)])
    con.commit()
    con.close()


def _legacy_repo(root: Path) -> Path:
    """A checkout with state inside it, git-tracking only what the repo ships."""
    root.mkdir()
    for d in ("memory", "projects", "agents"):
        (root / d).mkdir()
        (root / d / ".gitkeep").write_text("")
    (root / "skills" / "organize-project").mkdir(parents=True)
    (root / "skills" / "organize-project" / "SKILL.md").write_text("shipped")
    (root / "tools" / "t").mkdir(parents=True)
    (root / "tools" / "t" / "TOOL.md").write_text("code")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    # now the runtime state git ignores/never saw
    (root / "memory" / "soul.md").write_text("I am Jav3")
    (root / "memory" / "notes").mkdir()
    (root / "memory" / "notes" / "rule.md").write_text("be brief")
    (root / "projects" / "p" / "code").mkdir(parents=True)
    (root / "projects" / "p" / "project.md").write_text("# p")
    (root / "skills" / "mine").mkdir()
    (root / "skills" / "mine" / "SKILL.md").write_text("operator skill")
    (root / "agents" / "a").mkdir()
    (root / "agents" / "a" / "AGENT.md").write_text("agent")
    _db(root / "data" / "jarvis.db")
    (root / "data" / "jwt_secret").write_text("s3cret")
    (root / "data" / "vm").mkdir()
    (root / "data" / "vm" / "base-v1.qcow2").write_bytes(b"\0" * 4096)
    return root


# ---------------------------------------------------------------- derivation --

def test_dirs_derive_from_state_dir(tmp_path):
    s = mk(base_dir=tmp_path / "repo", state_dir=tmp_path / "state")
    st = tmp_path / "state"
    assert s.data_dir == st / "data"
    assert s.db_path == st / "data" / "jarvis.db"
    assert s.vm_dir == st / "data" / "vm"
    assert (s.memory_dir, s.projects_dir, s.skills_dir, s.agents_dir) == (
        st / "memory", st / "projects", st / "skills", st / "agents")
    # tools are code: they stay with the checkout
    assert s.tools_dir == config.BASE_DIR / "tools"
    assert not s.legacy_layout


def test_state_dir_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "st"))
    s = mk(base_dir=tmp_path / "repo")
    assert s.memory_dir == tmp_path / "st" / "memory"


def test_explicit_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_DIR", str(tmp_path / "elsewhere"))
    s = mk(base_dir=tmp_path / "repo", state_dir=tmp_path / "state",
           data_dir=tmp_path / "d")
    assert s.memory_dir == tmp_path / "elsewhere"
    assert s.data_dir == tmp_path / "d"
    # db + vm follow an explicit data_dir unless set themselves
    assert s.db_path == tmp_path / "d" / "jarvis.db"
    assert s.vm_dir == tmp_path / "d" / "vm"
    s = mk(base_dir=tmp_path / "repo", state_dir=tmp_path / "state",
           db_path=tmp_path / "x.db")
    assert s.db_path == tmp_path / "x.db"
    assert s.projects_dir == tmp_path / "state" / "projects"


# ------------------------------------------------------------------ legacy ---

def test_legacy_layout_keeps_running_from_checkout(tmp_path, monkeypatch, caplog):
    repo = _legacy_repo(tmp_path / "repo")
    s = mk(base_dir=repo, state_dir=tmp_path / "state")
    assert s.legacy_layout
    assert s.db_path == repo / "data" / "jarvis.db"
    assert s.memory_dir == repo / "memory"
    assert s.vm_dir == repo / "data" / "vm"

    monkeypatch.setattr(config, "settings", s)
    monkeypatch.setattr(config, "_warned_legacy", False)
    with caplog.at_level(logging.WARNING, logger="backend.config"):
        config.ensure_dirs()
        config.ensure_dirs()
    warns = [r for r in caplog.records if "migrate-state" in r.getMessage()]
    assert len(warns) == 1            # one clear warning, not one per call
    assert not (tmp_path / "state").exists()   # nothing moved or created there


def test_fresh_checkout_is_not_legacy(tmp_path):
    repo = tmp_path / "repo"
    (repo / "memory").mkdir(parents=True)
    (repo / "memory" / ".gitkeep").write_text("")
    (repo / "skills" / "organize-project").mkdir(parents=True)
    (repo / "skills" / "organize-project" / "SKILL.md").write_text("shipped")
    assert not mk(base_dir=repo, state_dir=tmp_path / "state").legacy_layout


def test_populated_state_dir_wins_over_legacy(tmp_path):
    repo = _legacy_repo(tmp_path / "repo")
    st = tmp_path / "state"
    _db(st / "data" / "jarvis.db")
    s = mk(base_dir=repo, state_dir=st)
    assert not s.legacy_layout
    assert s.db_path == st / "data" / "jarvis.db"


def test_shipped_skills_seed_once(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "skills" / "organize-project").mkdir(parents=True)
    (repo / "skills" / "organize-project" / "SKILL.md").write_text("shipped")
    s = mk(base_dir=repo, state_dir=tmp_path / "state")
    monkeypatch.setattr(config, "settings", s)
    config.ensure_dirs()
    seeded = s.skills_dir / "organize-project" / "SKILL.md"
    assert seeded.read_text() == "shipped"
    seeded.write_text("edited")
    config.ensure_dirs()
    assert seeded.read_text() == "edited"        # never overwritten
    import shutil
    shutil.rmtree(s.skills_dir / "organize-project")
    config.ensure_dirs()
    assert not seeded.exists()                   # a deletion sticks


# ----------------------------------------------------------------- migrate ---

@pytest.fixture
def legacy(tmp_path, monkeypatch):
    repo = _legacy_repo(tmp_path / "repo")
    s = mk(base_dir=repo, state_dir=tmp_path / "state")
    monkeypatch.setattr(statemigrate, "settings", s)
    monkeypatch.setattr(statemigrate, "_unit_active", lambda: False)
    return repo, tmp_path / "state"


def test_migrate_moves_state_and_verifies(legacy):
    repo, st = legacy
    lines = statemigrate.migrate_state()
    assert any("verified" in line for line in lines)

    assert (st / "memory" / "soul.md").read_text() == "I am Jav3"
    assert (st / "memory" / "notes" / "rule.md").exists()
    assert (st / "projects" / "p" / "project.md").exists()
    assert (st / "projects" / "p" / "code").is_dir()
    assert (st / "agents" / "a" / "AGENT.md").exists()
    assert (st / "skills" / "mine" / "SKILL.md").exists()
    assert (st / "skills" / "organize-project" / "SKILL.md").exists()
    assert (st / "data" / "jwt_secret").read_text() == "s3cret"
    assert (st / "data" / "vm" / "base-v1.qcow2").stat().st_size == 4096

    con = sqlite3.connect(st / "data" / "jarvis.db")
    assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 3
    assert con.execute("SELECT path FROM projects").fetchone()[0] == str(
        st.resolve() / "projects" / "p")
    con.close()

    # source: runtime state gone, shipped/tracked files kept
    assert not (repo / "data").exists()
    assert not (repo / "memory" / "soul.md").exists()
    assert not (repo / "projects" / "p").exists()
    assert not (repo / "agents" / "a").exists()
    assert not (repo / "skills" / "mine").exists()
    assert (repo / "skills" / "organize-project" / "SKILL.md").exists()
    assert (repo / "memory" / ".gitkeep").exists()
    assert (repo / "tools" / "t" / "TOOL.md").exists()

    # the app now resolves the new place, and not as a legacy layout
    s = mk(base_dir=repo, state_dir=st)
    assert not s.legacy_layout and s.db_path == st / "data" / "jarvis.db"


def test_migrate_to_explicit_dir(legacy, tmp_path):
    repo, _ = legacy
    lines = statemigrate.migrate_state(tmp_path / "other")
    assert (tmp_path / "other" / "memory" / "soul.md").exists()
    assert any("JARVIS_STATE_DIR" in line for line in lines)


def test_migrate_refuses_while_service_runs(legacy, monkeypatch):
    repo, st = legacy
    monkeypatch.setattr(statemigrate, "_unit_active", lambda: True)
    with pytest.raises(statemigrate.MigrateError, match="running"):
        statemigrate.migrate_state()
    assert (repo / "memory" / "soul.md").exists() and not st.exists()


def test_migrate_refuses_on_locked_db(legacy):
    repo, st = legacy
    holder = sqlite3.connect(repo / "data" / "jarvis.db")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(statemigrate.MigrateError, match="locked|reader"):
            statemigrate.migrate_state()
    finally:
        holder.rollback()
        holder.close()
    assert (repo / "data" / "jarvis.db").exists() and not st.exists()


def test_migrate_refuses_into_populated_target(legacy):
    repo, st = legacy
    (st / "memory").mkdir(parents=True)
    (st / "memory" / "soul.md").write_text("other")
    with pytest.raises(statemigrate.MigrateError, match="already holds"):
        statemigrate.migrate_state()
    assert (repo / "memory" / "soul.md").exists()


def test_migrate_nothing_to_do(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(statemigrate, "settings",
                        mk(base_dir=repo, state_dir=tmp_path / "state"))
    with pytest.raises(statemigrate.MigrateError, match="nothing to migrate"):
        statemigrate.migrate_state()
