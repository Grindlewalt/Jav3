"""The research final-doc write path: staged, or auto-approved to canonical."""
from backend.config import settings
from backend.research import _write_doc


def _mkproject(name: str = "demo") -> None:
    (settings.projects_dir / name).mkdir(parents=True, exist_ok=True)


def test_write_doc_auto_approved(tmp_env, monkeypatch):
    _mkproject()
    monkeypatch.setattr(settings, "research_auto_approve", True)
    status = _write_doc("demo", "research/topic.md", "# findings")
    assert status == "canonical"
    assert (settings.projects_dir / "demo" / "research" / "topic.md"
            ).read_text() == "# findings"
    # nothing lingers in the approval queue
    assert not (settings.projects_dir / "demo" / ".staging").exists()


def test_write_doc_staged_when_flag_off(tmp_env, monkeypatch):
    _mkproject()
    monkeypatch.setattr(settings, "research_auto_approve", False)
    status = _write_doc("demo", "research/topic.md", "# findings")
    assert status == "staged"
    assert (settings.projects_dir / "demo" / ".staging" / "research" / "topic.md"
            ).read_text() == "# findings"
    # canonical file untouched until the operator approves
    assert not (settings.projects_dir / "demo" / "research" / "topic.md").exists()
