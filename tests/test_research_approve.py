"""The research final-doc write path: written straight to the project."""
from backend.config import settings
from backend.research import _write_doc


def _mkproject(name: str = "demo") -> None:
    (settings.projects_dir / name).mkdir(parents=True, exist_ok=True)


async def test_write_doc_lands_canonical(tmp_env):
    _mkproject()
    status = await _write_doc("demo", "research/topic.md", "# findings")
    assert status == "canonical"
    assert (settings.projects_dir / "demo" / "research" / "topic.md"
            ).read_text() == "# findings"
    # no quarantine dir appears
    assert not (settings.projects_dir / "demo" / ".staging").exists()
