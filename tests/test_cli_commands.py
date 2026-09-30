"""Slash commands in the terminal client (clients/jav3cli/jav3): Enter on a bare
/command runs it (its bare form opens the picker) instead of turning into a
second, different command; /project checks the name (TUI-02, TUI-03)."""
import pytest

from cli_fake import FakeServer, load_client, top, wait_for

jav3 = load_client("jav3cli_commands")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "cfg" / "jav3"


def notes(app) -> list[str]:
    return [str(n.render()) for n in app.query("Note")]


async def boot(size=(120, 40), **kw):
    srv = FakeServer(**kw)
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    return srv, app


async def type_line(pilot, text: str) -> None:
    await pilot.press(*[("space" if c == " " else c) for c in text])
    await pilot.pause(0.1)


@pytest.mark.parametrize("typed, title", [
    ("/models", "Models"), ("/mod", "Models"), ("/orchestration", "Orchestrate which project?"),
    ("/project", "Projects"), ("/agents", "Agents"), ("/sessions", "Sessions"),
    ("/themes", "Themes")])
async def test_enter_on_a_bare_command_runs_its_picker(typed, title):
    srv, app = await boot(projects=["alpha", "beta"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, typed)
        assert app.popup_open()
        await pilot.press("enter")
        assert await wait_for(lambda: top(app) in ("Picker", "ThemePicker", "BrainDump"))
        assert top(app) != "BrainDump"                  # not the first project's brain dump
        assert app.screen.title_text == title
        assert app.editor.text == ""
        await pilot.press("escape")


async def test_enter_on_bare_export_runs_export_not_its_help_line():
    srv, app = await boot()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, "/export")
        await pilot.press("enter")
        assert await wait_for(lambda: any("no chat to export" in n for n in notes(app)))
        assert not any("saves to" in n for n in notes(app))
        assert app.editor.text == ""
        assert app.history[-1] == "/export"


async def test_a_typed_space_then_enter_is_still_the_bare_command():
    srv, app = await boot()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, "/models ")
        assert app.popup_open() and app.popup_items[0][0] == "arg"
        await pilot.press("enter")
        assert await wait_for(lambda: top(app) == "Picker")
        assert app.screen.title_text == "Models" and app.model is None


async def test_tab_opens_the_argument_menu_and_enter_then_picks_from_it():
    srv, app = await boot()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, "/mo")
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert app.editor.text == "/models " and app.popup_items[0] == ("arg", "deepseek/deepseek-flash")
        await pilot.press("enter")
        assert await wait_for(lambda: app.model == "deepseek/deepseek-flash")
        assert app.editor.text == "" and top(app) == "Screen"


async def test_arrowing_into_the_menu_makes_enter_take_the_row():
    srv, app = await boot(projects=["alpha", "beta"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, "/project ")
        await pilot.press("down")
        await pilot.press("enter")
        assert await wait_for(lambda: app.project_mode == "follow")     # row 2 of none / follow / ...
        assert top(app) == "Screen"


async def test_an_exact_argument_comes_before_the_ones_that_only_begin_with_it():
    srv, app = await boot(projects=["alpha2", "alpha"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        await type_line(pilot, "/project alpha")
        assert app.popup_items[0] == ("arg", "alpha") and ("arg", "alpha2") in app.popup_items
        await pilot.press("enter")
        assert await wait_for(lambda: app.project == "alpha")


# --- /project: a name that is not a project (TUI-03) -----------------------------------------

async def test_project_refuses_a_name_that_does_not_exist_and_lists_the_real_ones():
    srv, app = await boot(projects=["alpha", "beta"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/project brandnew")
        assert await wait_for(lambda: any("no project named 'brandnew'" in n for n in notes(app)))
        text = " ".join(notes(app))
        assert "alpha, beta" in text and "/login" in text
        assert app.project is None and app.project_mode != "pin"


async def test_project_takes_a_slug_a_display_name_or_a_new_one_made_elsewhere():
    srv, app = await boot(projects=["alpha", "beta"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/project ALPHA")
        assert await wait_for(lambda: app.project == "alpha")
        srv.projects.append("gamma")                    # made in the web app meanwhile
        app.dispatch("/project gamma")
        assert await wait_for(lambda: app.project == "gamma")


async def test_the_pickers_typed_name_is_checked_the_same_way():
    srv, app = await boot(projects=["alpha"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.3)
        app.dispatch("/project")
        assert await wait_for(lambda: top(app) == "Picker")
        await pilot.press(*"brandnew")
        await pilot.press("enter")
        assert await wait_for(lambda: any("no project named" in n for n in notes(app)))
        assert app.project is None


async def _full_app(projects):
    srv = FakeServer(projects=projects, full=True)
    app = jav3.build_tui("http://h:1", jav3.SESSION_PREFIX + "jwt", transport=srv.transport())
    return srv, app


async def test_with_full_access_an_unknown_name_offers_to_create_it():
    srv, app = await _full_app(["alpha"])
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.4)
        assert app.full_access
        app.dispatch("/project brandnew")
        assert await wait_for(lambda: top(app) == "Confirm")
        assert "brandnew" in str(app.screen.question) and "Create" in str(app.screen.question)
        await pilot.press("n")
        await pilot.pause(0.3)
        assert app.project is None and srv.created == []
        app.dispatch("/project brandnew")
        assert await wait_for(lambda: top(app) == "Confirm")
        await pilot.press("y")
        assert await wait_for(lambda: app.project == "brandnew")
        assert srv.created == [{"name": "brandnew"}]
