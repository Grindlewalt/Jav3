"""The terminal client's themes (clients/jav3cli/jav3): the 256-colour mapping,
the readability checks every theme is held to, the /theme picker (live preview,
revert on esc, kept across restarts), the editor at 80x24, and export/import
round trips."""
import importlib.machinery
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_themes", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_themes", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()
textual = pytest.importorskip("textual")
rich_color = pytest.importorskip("rich.color")
from textual.theme import BUILTIN_THEMES, Theme  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "cfg" / "jav3"


def _server():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "m/x", "active_project": None,
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        return httpx.Response(404, json={"detail": "nope"})
    return httpx.MockTransport(handler)


def _app(colour: str = "256"):
    """The TUI on a terminal with 256 colours (Terminal.app) or truecolor."""
    from rich.color import ColorSystem
    app = jav3.build_tui("http://h:1", "jvd_x", transport=_server())
    app.console._color_system = (ColorSystem.TRUECOLOR if colour == "true"
                                 else ColorSystem.EIGHT_BIT)
    return app


async def _until(pilot, cond, tries=60):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(0.05)
    return cond()


async def _modal(pilot, app, name):
    return await _until(pilot, lambda: type(app.screen).__name__ == name)


def _all_themes() -> dict:
    """Every theme the app offers, as Textual objects, tuned as the app tunes them."""
    out = {n: replace(BUILTIN_THEMES[n], **jav3.THEME_TUNE.get(n, {})) for n in jav3.THEME_KEEP}
    for spec in jav3.jav3_theme_specs(True):
        out[spec["name"]] = Theme(**spec)
    out["jav3"] = Theme(name="jav3", primary="#5fd7af", secondary="#af87ff", accent="#ffaf5f",
                        foreground="#e4e4e4", background="#0e1014", surface="#161a20",
                        panel="#1e232b", warning="#ffaf00", error="#ff6b6b",
                        success="#5fd787", dark=True)
    return out


ALL = _all_themes()


# --- the 256-colour mapping ---------------------------------------------------------

def _rich_index(rgb) -> int:
    return rich_color.Color.from_rgb(*rgb).downgrade(rich_color.ColorSystem.EIGHT_BIT).number


def test_every_feed_colour_selects_the_palette_entry_it_stands_for():
    cands = jav3._xterm_colours()
    assert len(cands) > 200
    for shown, feed, _ in cands:
        assert tuple(rich_color.EIGHT_BIT_PALETTE[_rich_index(feed)]) == tuple(shown)


def test_snapping_keeps_slate_out_of_teal():
    # Rich alone turns nord's #2e3440 into (0, 95, 95); snapped it is a grey
    from textual.color import Color
    c = Color.parse("#2e3440")
    assert tuple(rich_color.EIGHT_BIT_PALETTE[_rich_index((c.r, c.g, c.b))]) == (0, 95, 95)
    feed = jav3._hex_rgb(jav3.snap_colour("#2e3440"))
    shown = tuple(rich_color.EIGHT_BIT_PALETTE[_rich_index(feed)])
    assert shown[0] == shown[1] == shown[2] and 30 <= shown[0] <= 60
    # a saturated colour stays that colour
    feed = jav3._hex_rgb(jav3.snap_colour("#ff6b6b"))
    r, g, b = rich_color.EIGHT_BIT_PALETTE[_rich_index(feed)]
    assert r > 200 and g < 150 and b < 150


@pytest.mark.parametrize("name", sorted(ALL))
def test_256_variant_is_palette_only_and_stable(name):
    src = jav3.resolved_theme_spec(ALL[name])
    once = jav3.terminal_theme_spec(src, False)
    if src["ansi"]:
        assert once is src                        # the terminal's own colours: left alone
        return
    palette = {tuple(sh) for sh, _, _ in jav3._xterm_colours()}
    for k in jav3.THEME_ROLES:
        assert tuple(rich_color.EIGHT_BIT_PALETTE[_rich_index(jav3._hex_rgb(once[k]))]) in palette
    again = jav3.terminal_theme_spec(once, False)
    assert {k: again[k] for k in jav3.THEME_ROLES} == {k: once[k] for k in jav3.THEME_ROLES}
    # the three surfaces are greys, each a clear step from the last
    steps = [jav3._lab(jav3._hex_rgb(once[k]))[0] for k in ("background", "surface", "panel")]
    assert abs(steps[1] - steps[0]) >= jav3.MIN_SURFACE_STEP - 0.5
    assert abs(steps[2] - steps[1]) >= jav3.MIN_SURFACE_STEP - 0.5
    assert (steps[1] - steps[0]) * (steps[2] - steps[1]) > 0        # one direction


def test_jav3_default_survives_snapping():
    src = jav3.resolved_theme_spec(ALL["jav3"])
    once = jav3.terminal_theme_spec(src, False)
    # the colours Rich already picked for it, now exact (#ff6b6b was drawn as #ff5f5f)
    assert (once["primary"], once["error"], once["success"]) == ("#5fd7af", "#ff5f5f", "#5fd787")
    assert (once["background"], once["surface"], once["panel"]) == ("#121212", "#262626", "#3a3a3a")


# --- readability, in words --------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(ALL))
def test_every_theme_reads_on_both_kinds_of_terminal(name):
    src = jav3.resolved_theme_spec(ALL[name])
    assert jav3.theme_problems(src) == [], name
    assert jav3.theme_problems(jav3.terminal_theme_spec(src, False)) == [], name


def test_theme_problems_names_what_reads_badly():
    fine = jav3.resolved_theme_spec(ALL["jav3"])
    assert jav3.theme_problems(fine) == []
    pale = {**fine, "warning": "#0e1015"}                      # the background, nearly
    assert any(p.startswith("warning is hard to read") for p in jav3.theme_problems(pale))
    same = {**fine, "surface": fine["background"]}
    assert any("background and surface are almost the same" in p
               for p in jav3.theme_problems(same))
    alike = {**fine, "success": "#ff6a6a"}
    assert any("error and success look alike" in p for p in jav3.theme_problems(alike))
    dim = {**fine, "primary": "#204060"}
    assert any("hard to read" in p for p in jav3.theme_problems(dim))


def test_muted_text_is_one_solid_readable_colour():
    for name in ALL:
        src = jav3.resolved_theme_spec(ALL[name])
        if src["ansi"]:
            continue
        for snap in (False, True):
            spec = jav3.terminal_theme_spec(src, not snap)
            h = jav3.muted_colour(spec, snap)
            assert len(h) == 7 and h[0] == "#"
            assert jav3._contrast(jav3._hex_rgb(h), jav3._hex_rgb(spec["background"])) >= 3.5, name


def test_diff_tint_is_a_flag_the_theme_sets():
    body = lambda: jav3.tool_body("edit_file", {"find": "a", "replace": "b"}, True, "ok", False)[0]
    assert "on $error 12%" in body()
    jav3.DIFF_TINT = False
    try:
        assert "12%" not in body() and "[$error]" in body()
    finally:
        jav3.DIFF_TINT = True


# --- the app: registration, picker, persistence ----------------------------------------------

async def test_themes_and_theme_are_one_command(cfg):
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.commands["themes"] is app.commands["theme"]
        assert [n for n, c in app.unique_commands() if c is app.commands["theme"]] == ["themes"]
        app.dispatch("/themes")
        assert await _modal(pilot, app, "ThemePicker")


async def test_256_terminal_registers_snapped_themes_but_exports_the_originals(cfg, tmp_path):
    app = _app("256")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        nord = app.available_themes["nord"]
        assert nord.background != "#2E3440" and nord.background.lower() == \
            jav3.terminal_theme_spec(jav3.resolved_theme_spec(BUILTIN_THEMES["nord"]),
                                     False)["background"]
        await app.c_theme("nord")
        await app.c_theme(f"export {tmp_path / 'n.json'}")
        out = json.loads((tmp_path / "n.json").read_text())
        assert out["name"] == "nord-copy" and out["primary"] == "#88c0d0"      # as written, not snapped
    app = _app("true")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.available_themes["nord"].background.lower() == "#2e3440"


async def test_theme_picker_previews_as_you_arrow_and_reverts_on_esc(cfg):
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.theme == "jav3"
        app.dispatch("/theme")
        assert await _modal(pilot, app, "ThemePicker")
        rows = app.theme_rows()
        assert rows[0][0] == "jav3" and rows[1][0].startswith("jav3-")       # Jav3's own first
        assert dict((v, m) for v, _, m in rows)["jav3-light"] == "light"
        seen = [app.theme]
        for _ in range(3):
            await pilot.press("down")
            await pilot.pause(0.1)
            seen.append(app.theme)
        assert len(set(seen)) == 4 and seen[-1] == rows[3][0]      # each arrow shows that theme
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.theme == "jav3")    # the old one is back
        assert "theme" not in json.loads((cfg / "tui.json").read_text()) \
            if (cfg / "tui.json").exists() else True                # and nothing was saved


async def test_theme_choice_is_kept_and_survives_a_restart(cfg):
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.dispatch("/theme")
        assert await _modal(pilot, app, "ThemePicker")
        await pilot.press("down", "down")
        await pilot.pause(0.1)
        picked = app.theme
        assert picked != "jav3"
        await pilot.press("enter")
        assert await _until(pilot, lambda: type(app.screen).__name__ != "ThemePicker")
        assert app.theme == picked
        assert json.loads((cfg / "tui.json").read_text())["theme"] == picked
    again = _app()
    async with again.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        assert again.theme == picked


async def test_a_theme_that_no_longer_exists_falls_back_to_jav3(cfg):
    cfg.mkdir(parents=True)
    (cfg / "tui.json").write_text(json.dumps({"theme": "gone"}))
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.theme == "jav3"


# --- the editor at 80x24 ------------------------------------------------------------------------

async def test_theme_editor_fits_80x24_and_the_mode_toggle_works(cfg):
    app = _app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        app.dispatch("/theme create")
        assert await _modal(pilot, app, "ThemeEditor")
        await pilot.pause(0.2)
        ed = app.screen
        for sel in ("#th-name", "#th-dark", "#th-primary", "#th-success", "#dialog-hint"):
            r = ed.query_one(sel).region
            assert r.height >= 1 and r.width >= 8, sel
            assert r.right <= 80 and r.bottom <= 24, sel               # nothing cut off
        toggle = ed.query_one("#th-dark")
        assert toggle.region.width >= 18                                  # room for both words
        assert "dark" in str(toggle.render()) and "light" in str(toggle.render())
        assert app.current_theme.dark is True
        await pilot.press("tab")                                          # name -> mode
        assert app.focused is toggle
        await pilot.press("space")
        await pilot.pause(0.1)
        assert toggle.dark is False and app.current_theme.dark is False   # previewed at once
        await pilot.press("left")
        assert toggle.dark is True
        await pilot.press("escape")
        assert await _until(pilot, lambda: app.theme == "jav3")


async def test_theme_editor_hint_warns_about_hard_to_read_colours(cfg):
    app = _app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        app.dispatch("/theme create")
        assert await _modal(pilot, app, "ThemeEditor")
        ed = app.screen
        for k, v in (("background", "#ffffff"), ("surface", "#eeeeee"), ("panel", "#dddddd"),
                     ("foreground", "#202020"), ("primary", "#204080"), ("secondary", "#204080"),
                     ("accent", "#204080"), ("error", "#a01020"), ("success", "#106030"),
                     ("warning", "#ffff00")):
            ed.query_one(f"#th-{k}").value = v
        await pilot.pause(0.2)
        hint = str(ed.query_one("#dialog-hint").render())
        assert "previewing" in hint and "⚠ warning is hard to read" in hint
        ed.query_one("#th-warning").value = "#8a5a00"
        await pilot.pause(0.2)
        hint = str(ed.query_one("#dialog-hint").render())
        assert "previewing" in hint and "⚠" not in hint
        await pilot.press("escape")


# --- export and import round trips -------------------------------------------------------------------

@pytest.mark.parametrize("colour", ["256", "true"])
async def test_every_theme_exports_and_imports_back_the_same(cfg, tmp_path, colour):
    app = _app(colour)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        names = [n for n in app.theme_names() if n in app.builtin_themes]
        assert len(names) >= 15
        for name in names:
            if BUILTIN_THEMES.get(name) is not None and BUILTIN_THEMES[name].ansi:
                continue                               # ANSI names are not #rrggbb: not exportable
            out = tmp_path / f"{name}.json"
            await app.c_theme(name)
            await app.c_theme(f"export {out}")
            exported = json.loads(out.read_text())
            assert exported["name"] == name[:35] + "-copy"
            assert exported["primary"].startswith("#")
            # import it back under a fresh name: same colours, and it becomes the theme
            exported["name"] = "rt " + name
            out.write_text(json.dumps(exported))
            await app.c_theme(f"import {out}")
            assert app.theme == "rt " + name
            back = app.current_theme_dict()
            assert back == {**exported, "name": "rt " + name}


async def test_a_created_theme_survives_export_delete_import(cfg, tmp_path):
    app = _app()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.dispatch("/theme create")
        assert await _modal(pilot, app, "ThemeEditor")
        ed = app.screen
        ed.query_one("#th-name").value = "Round trip"
        ed.query_one("#th-primary").value = "#336699"
        ed.query_one("#th-warning").value = "#cc8800"
        await pilot.press("ctrl+s")
        assert await _until(pilot, lambda: app.theme == "Round trip")
        stored = cfg / "themes" / "round-trip.json"
        original = json.loads(stored.read_text())
        assert original["primary"] == "#336699" and original["warning"] == "#cc8800"
        await app.c_theme(f"export {tmp_path / 'rt.json'}")
        exported = json.loads((tmp_path / "rt.json").read_text())
        assert exported == original                       # what was typed, whatever the terminal
        stored.unlink()
    again = _app()                                        # a restart without it: gone
    async with again.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert "Round trip" not in again.theme_names() and again.theme == "jav3"
        await again.c_theme(f"import {tmp_path / 'rt.json'}")
        assert again.theme == "Round trip"
        assert json.loads(stored.read_text()) == original
    third = _app()                                        # and it is kept from then on
    async with third.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        assert third.theme == "Round trip"


# --- what a real 256-colour terminal is sent -------------------------------------------------------------

def _screen_colours(theme: str, colorterm: bool, tmp_path):
    """The TUI in a pseudo-terminal (logged out, so it needs no server): the
    background colour pyte sees, and the colour of the logo."""
    pexpect = pytest.importorskip("pexpect")
    pyte = pytest.importorskip("pyte")
    import os
    import sys
    import time
    d = tmp_path / "cfg" / "jav3"
    d.mkdir(parents=True)
    (d / "tui.json").write_text(json.dumps({"theme": theme}))
    env = {k: v for k, v in os.environ.items()
           if k not in ("COLORTERM", "NO_COLOR", "FORCE_COLOR")}
    env.update(XDG_CONFIG_HOME=str(tmp_path / "cfg"), HOME=str(tmp_path), TERM="xterm-256color")
    if colorterm:
        env["COLORTERM"] = "truecolor"
    screen = pyte.Screen(100, 30)
    stream = pyte.ByteStream(screen)
    child = pexpect.spawn(sys.executable, [str(CLI)], env=env, dimensions=(30, 100),
                          cwd=str(tmp_path))
    try:
        end = time.time() + 4
        while time.time() < end:
            try:
                stream.feed(child.read_nonblocking(65536, timeout=0.1))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
    finally:
        child.terminate(force=True)
    return screen.buffer[0][50].bg, screen.buffer[2][50].bg


def test_nord_on_a_256_terminal_is_grey_not_teal(tmp_path):
    bg, _ = _screen_colours("nord", False, tmp_path)
    r, g, b = (int(bg[i:i + 2], 16) for i in (0, 2, 4))
    assert r == g == b and 30 <= r <= 60, bg               # was 005f5f before the snapping


def test_nord_on_a_truecolor_terminal_is_as_written(tmp_path):
    bg, _ = _screen_colours("nord", True, tmp_path)
    assert bg == "2e3440"
