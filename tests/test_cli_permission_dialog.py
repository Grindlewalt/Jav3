"""The permission dialog (clients/jav3cli/jav3, AskUser): it names the agent that asks,
says a command or a path once, numbers what the keys pick, and keeps its options and
key line on screen at 80x24 whatever the command is (TUI-06)."""
import pytest

from cli_fake import FakeServer, finish, load_client, open_chat, top, wait_for

jav3 = load_client("jav3cli_permdlg")


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path / "cfg" / "jav3"


def perm(tool, question, detail, aid="ask_p", agent="Write c.txt", cid=601):
    return {"type": "ask_user", "id": aid, "conversation_id": cid, "agent": agent,
            "kind": "permission", "tool": tool, "reason": "ask mode", "detail": detail,
            "free_text_label": "No, tell the agent what to do instead",
            "questions": [{"question": question, "multi_select": False,
                           "options": ["Yes", "Yes, always allow this and similar commands "
                                              f"({tool}: x)"]}]}


def test_a_command_is_said_once():
    cmd = "cd . && md5sum one.txt && printf 'delta' > d.txt"
    ev = perm("run_code", f"Run in the VM: {cmd}", cmd)
    head, detail = jav3.ask_view(ev, ev["questions"][0]["question"])
    assert head == "Run in the VM" and cmd in detail
    # the headline cuts a long command at 160 characters: still one command, said once
    long = "echo " + "x" * 400
    ev = perm("run_code", "Run in the VM: " + long[:160], long)
    assert jav3.ask_view(ev, ev["questions"][0]["question"])[0] == "Run in the VM"
    # a detail that is something else keeps the headline
    ev = perm("run_code", "Run in the VM: ls", "something else entirely")
    assert jav3.ask_view(ev, ev["questions"][0]["question"])[0].endswith(": ls")


def test_a_write_says_the_path_once_and_labels_the_contents():
    ev = perm("write_file", "Write z.txt", "z.txt\nz")
    head, detail = jav3.ask_view(ev, "Write z.txt")
    assert head == "Write z.txt"
    assert detail.splitlines() == ["[$text-muted]contents[/]", "[$text-muted]z[/]"]
    ev = perm("write_file", "Write z.txt", "z.txt\n")
    assert "(empty file)" in jav3.ask_view(ev, "Write z.txt")[1]


def test_an_edit_shows_old_and_new_text_in_red_and_green():
    ev = perm("edit_file", "Edit a.py", "a.py\n- x = 1\n+ x = 2")
    head, detail = jav3.ask_view(ev, "Edit a.py")
    lines = detail.splitlines()
    assert lines == ["[$error]- x = 1[/]", "[$success]+ x = 2[/]"]


def test_other_asks_and_long_details_are_left_alone_or_cut():
    ev = {"kind": "question", "questions": [{"question": "Which port?", "options": ["1", "2"]}]}
    assert jav3.ask_view(ev, "Which port?") == ("Which port?", "")
    ev = perm("run_code", "Run in the VM: x", "x" + "\n" + "y" * 5000)
    assert jav3.ask_view(ev, "Run in the VM: x")[1].endswith("…[/]")


async def test_the_dialog_says_who_asks_and_numbers_its_options():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        srv.feed.put(perm("write_file", "Write z.txt", "z.txt\nz"))
        assert await wait_for(lambda: top(app) == "AskUser")
        scr = app.screen
        type(scr).GRACE = 0
        head, rows = scr.markup(scr.ev, scr.qs, 0, 0, set(), "", scr.free_label, scr.who, 2)
        assert "Permission" in head and "ask mode" in head
        assert "Write c.txt #601" in head and "2 more waiting" in head
        assert head.count("z.txt") == 1                 # the path, once
        text = scr._foot_text()
        assert "1-2 pick" in text and "esc skips" in text
        assert "1. ( ) Yes" in rows.splitlines()[0] and "2. ( ) Yes, always" in rows
        # the free-text line appears only when it is in use
        assert not scr.query_one("#ask-text").display
        await pilot.press("down", "down")
        await pilot.pause(0.1)
        assert scr.query_one("#ask-text").display
        await pilot.press("escape")
        assert await wait_for(lambda: srv.answers)
        await finish(srv, app)


async def test_a_question_from_this_chat_says_so():
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await open_chat(pilot, app, srv)
        ev = perm("write_file", "Write z.txt", "z.txt\nz", agent="", cid=4)
        srv.feed.put(ev)
        assert await wait_for(lambda: top(app) == "AskUser")
        type(app.screen).GRACE = 0
        assert app.screen.who == "this chat"
        srv.feed.put({"type": "ask_user", "id": "ask_q", "conversation_id": 9,
                      "questions": [{"question": "Which port?", "options": ["1", "2"]}]})
        await pilot.pause(0.2)
        await pilot.press("escape")
        assert await wait_for(lambda: top(app) == "AskUser" and app.screen.ev["id"] == "ask_q")
        assert app.screen.who == "agent #9"             # no title from an older server
        await pilot.press("escape")
        await finish(srv, app)


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (60, 20)])
async def test_options_and_key_line_stay_on_screen_whatever_the_command(size):
    srv = FakeServer()
    app = jav3.build_tui("http://h:1", "jvd_x", transport=srv.transport())
    async with app.run_test(size=size) as pilot:
        await open_chat(pilot, app, srv)
        cmd = "\n".join(f"echo line {i} of a very long script that keeps going" for i in range(60))
        srv.feed.put(perm("run_code", "Run in the VM: " + " ".join(cmd.split())[:160], cmd))
        assert await wait_for(lambda: top(app) == "AskUser")
        scr = app.screen
        type(scr).GRACE = 0
        await pilot.pause(0.4)
        dialog = scr.query_one("#dialog").region
        for wid in ("#ask-opts", "#ask-foot"):
            r = scr.query_one(wid).region
            assert dialog.contains_region(r), (wid, r, dialog)
        assert dialog.bottom <= size[1] and dialog.y >= 0
        assert "esc skips" in scr._foot_text()
        # the command scrolls inside its own box
        top_box = scr.query_one("#ask-top")
        assert top_box.max_scroll_y > 0
        await pilot.press("pagedown")
        await pilot.pause(0.1)
        assert top_box.scroll_y > 0
        await pilot.press("escape")
        await finish(srv, app)
