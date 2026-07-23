"""GUI control channel: layout mutation helpers, media-source resolution, and
the self_docs sectioner. (The SSE plumbing reuses the proven bus pattern and is
exercised live on the Pi.)"""
import json

import pytest

from backend import gui
from backend.config import settings


@pytest.fixture
def proj(tmp_env):
    (settings.projects_dir / "demo").mkdir(parents=True)
    (settings.projects_dir / "demo" / "song.mp3").write_bytes(b"x")
    return "demo"


def test_default_panels_on_fresh_project(proj):
    panels = gui.load_panels(proj)
    assert {p["type"] for p in panels} == {"chat", "board", "git", "network"}


def test_add_remove_roundtrip(proj):
    panels = gui.load_panels(proj)
    added = gui.add_panel(panels, "secrets")
    assert added["id"] not in {"p1", "p2", "p3", "p4"}
    assert added["x"] >= max(p["x"] + p["w"] for p in panels[:-1])
    gui.save_panels(proj, panels)
    again = gui.load_panels(proj)
    assert any(p["type"] == "secrets" for p in again)
    kept, dropped = gui.remove_panels(again, "secrets")
    assert dropped == 1 and not any(p["type"] == "secrets" for p in kept)


def test_tile_no_overlap_and_size_preserved(proj):
    panels = gui.load_panels(proj)
    for _ in range(4):
        gui.add_panel(panels, "todos")
    tiled = gui.tile_panels(panels)
    assert len(tiled) == len(panels)
    for p in tiled:
        assert p["x"] + p["w"] <= gui.ROW_WIDTH or p["x"] == 16
    for a in tiled:
        for b in tiled:
            if a["id"] == b["id"]:
                continue
            overlap = (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"]
                       and a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"])
            assert not overlap, f"{a['id']} overlaps {b['id']}"


def test_media_src_project_file(proj):
    src, err = gui.media_src("song.mp3", proj)
    assert err is None and src == "/api/projects/demo/raw/song.mp3"


def test_media_src_missing_file(proj):
    src, err = gui.media_src("nope.mp3", proj)
    assert src is None and "no such file" in err


def test_media_src_escape_refused(proj):
    src, err = gui.media_src("../../etc/passwd", proj)
    assert src is None


def test_media_src_url_allowlist(proj, monkeypatch):
    monkeypatch.setattr(settings, "media_hosts", ["ok.example"])
    src, err = gui.media_src("https://cdn.ok.example/a.mp3", proj)
    assert err is None and src.startswith("https://cdn.ok.example")
    src, err = gui.media_src("https://evil.example/a.mp3", proj)
    assert src is None and "allowlist" in err


def test_media_src_no_project_needs_url(tmp_env):
    src, err = gui.media_src("song.mp3", None)
    assert src is None and "no active project" in err


async def test_self_docs_sections():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "self_docs" / "handler.py"
    spec = importlib.util.spec_from_file_location("t_self_docs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    listing = await mod.run()
    assert "- secrets" in listing and "- gui" in listing
    sec = await mod.run(section="secrets")
    assert sec.startswith("## secrets") and "{{secret:NAME}}" in sec
    assert "no section" in await mod.run(section="bogus")
