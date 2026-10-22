"""/security Queue: environment requests and skill install requests."""
import json

import httpx
import pytest

from test_cli import _open_security, _posts, _rows, _security_server, _text, _until, cfg, jav3  # noqa: F401

ENV = {"id": 4, "project_slug": "frc-sim", "name": "sim-gl", "from_variant": "desktop",
       "min_mem_mb": 1536, "reason": "needs [b]GL[/] now", "status": "pending",
       "created_at": "2026-10-22 18:02:11", "resolved": True,
       "packages": [{"id": 31, "manager": "apt", "package": "mesa-utils",
                     "resolved_version": "9.0.0-2"},
                    {"id": 32, "manager": "pip", "package": "pygame",
                     "resolved_version": "2.6.1"}],
       "plan": "builds image `sim-gl` from `desktop`; other projects unchanged"}
SKILL = {"id": 2, "project_slug": "frc-sim", "source": "https://github.com/x/skills#pdf",
         "reason": "fill forms", "status": "pending", "created_at": "2026-10-22 18:03:00"}
FETCHED = {**SKILL, "status": "fetched", "skill_name": "pdf", "ref": "3f9c",
           "files": {"SKILL.md": "ab"}, "description": "pdf tools", "skill_sha256": "ab12",
           "flags": [], "requirements": [{"reason": "needs poppler", "met": False}],
           "skill_md": "---\nname: pdf\n---\n[b]bold[/] [red]x[/red]"}


def _server(seen, envs=True, skills=True):
    inner = _security_server(seen)
    state = {"skills": [SKILL]}

    def handler(request):
        p, m = request.url.path, request.method
        if envs and p == "/api/environments" and m == "GET":
            seen.append((m, p, {}, None))
            return httpx.Response(200, json={"environments": [ENV]})
        if skills and p == "/api/skill-requests" and m == "GET":
            seen.append((m, p, {}, None))
            return httpx.Response(200, json={"requests": state["skills"]})
        if m == "POST" and (p.startswith("/api/environments/")
                            or p.startswith("/api/skill-requests/")):
            seen.append((m, p, {}, json.loads(request.content) if request.content else None))
            if p.endswith("/fetch"):
                state["skills"] = [FETCHED]
            return httpx.Response(200, json={"ok": True})
        return inner.handle_request(request)
    return httpx.MockTransport(handler)


async def _queue(pilot, app):
    scr = await _open_security(pilot, app)
    assert await _until(pilot, lambda: scr.loaded["queue"])
    await pilot.pause(0.1)
    return scr


def _sel(scr, prefix):
    scr.sel["queue"] = next(e["key"] for e in scr.entries["queue"]
                            if e["key"].startswith(prefix))
    scr.render_chrome()


async def test_rows_and_escaping(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _queue(pilot, app)
        rows = "\n".join(_rows(scr))
        assert "sim-gl" in rows and "2 packages" in rows and "frc-sim" in rows
        assert "SKILL" in rows and "github.com/x/skills" in rows
        _sel(scr, "ev")
        d = scr.detail_markup(scr.selected())
        assert "needs \\[b]GL" in d
        assert "9.0.0-2" in d and "1536" in d and "other projects: unchanged" in d


async def test_skill_fetch_then_install(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _queue(pilot, app)
        _sel(scr, "sk")
        assert "fetch" in scr.foot_keys()
        await pilot.press("y")
        assert await _until(pilot, lambda: type(app.top).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: ("POST", "/api/skill-requests/2/fetch", None)
                            in _posts(seen))
        assert await _until(pilot, lambda: app.top is scr and any(
            e["raw"].get("status") == "fetched" for e in scr.entries["queue"]))
        _sel(scr, "sk")
        d = scr.detail_markup(scr.selected())
        assert "UNMET" in d and "\\[b]bold" in d and "ab12" in d
        assert "install & grant" in scr.foot_keys()
        await pilot.press("y")
        assert await _until(pilot, lambda: type(app.top).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: (
            "POST", "/api/skill-requests/2/approve",
            {"acknowledge": True, "skill_sha256": "ab12"}) in _posts(seen))


async def test_env_approve_sends_build(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _queue(pilot, app)
        _sel(scr, "ev")
        await pilot.press("y")
        assert await _until(pilot, lambda: type(app.top).__name__ == "Confirm")
        await pilot.press("y")
        assert await _until(pilot, lambda: (
            "POST", "/api/environments/4/approve",
            {"acknowledge": True, "build": True}) in _posts(seen))


async def test_unresolved_env_refused(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess", transport=_server(seen))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _queue(pilot, app)
        _sel(scr, "ev")
        scr.selected()["raw"]["resolved"] = False
        await pilot.press("y")
        await pilot.pause(0.3)
        assert type(app.top).__name__ == "SecurityPage"
        assert not any("/approve" in p for _, p, _ in _posts(seen))


async def test_404_on_new_routes_keeps_queue(cfg):
    pytest.importorskip("textual")
    seen: list = []
    app = jav3.build_tui("http://h:1", "session:sess",
                         transport=_server(seen, envs=False, skills=False))
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.pause(0.3)
        scr = await _queue(pilot, app)
        assert "evil.example" in "\n".join(_rows(scr))
