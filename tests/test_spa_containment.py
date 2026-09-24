"""The SPA catch-all must never serve a file outside frontend/dist.

Regression for the 2026-09 review finding: `dist / full_path` served
`/../data/jwt_secret` (Starlette keeps `..`) and `//etc/hostname` (an
absolute segment discards the base) to unauthenticated callers.
"""
from pathlib import Path

from backend import main
from backend.config import settings


def test_dist_file_refuses_escapes(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>")
    (dist / "assets" / "app.js").write_text("js")
    (tmp_path / "secret").write_text("nope")
    monkeypatch.setattr(settings, "frontend_dist", dist)

    assert main.dist_file("assets/app.js") == (dist / "assets" / "app.js")
    assert main.dist_file("index.html") == dist / "index.html"
    assert main.dist_file("") is None
    assert main.dist_file("missing.js") is None
    assert main.dist_file("assets") is None            # a directory
    assert main.dist_file("../secret") is None
    assert main.dist_file("assets/../../secret") is None
    assert main.dist_file("/etc/hostname") is None
    assert main.dist_file("//etc/hostname") is None
    assert main.dist_file(str(tmp_path / "secret")) is None


def test_dist_file_refuses_symlink_out(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (tmp_path / "secret").write_text("nope")
    (dist / "leak").symlink_to(tmp_path / "secret")
    monkeypatch.setattr(settings, "frontend_dist", dist)
    assert main.dist_file("leak") is None
