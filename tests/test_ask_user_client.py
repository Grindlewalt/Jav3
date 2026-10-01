"""ask_user, client side: the jav3 TUI's AskUser dialog answering an
`ask_user` event with POST /api/chat/{id}/answer (keys, skip, free text)."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import httpx

CLI = Path(__file__).resolve().parents[1] / "clients" / "jav3cli" / "jav3"


def _load():
    loader = importlib.machinery.SourceFileLoader("jav3cli_ask", str(CLI))
    spec = importlib.util.spec_from_loader("jav3cli_ask", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jav3 = _load()

QS = [{"question": "Which database?", "options": ["SQLite", "Postgres"],
       "multi_select": False},
      {"question": "Extras?", "options": ["auth", "admin", "api"], "multi_select": True}]


def check_markup(AskUser):
    head, rows = AskUser.markup({"kind": "permission", "reason": "judged RISKY"},
                                     QS, 1, 3, {0, 2}, "docs", "Type something…")
    assert "Permission" in head and "2/2" in head and "Extras?" in head
    lines = rows.splitlines()
    assert lines[0].startswith("1. [x] auth") and lines[1].startswith("2. [ ] admin")
    assert "[reverse]" in lines[3] and "[x]" in lines[3] and "Type something" in lines[3]


async def _run(keys, events_extra=(), check=None):
    posted, answers = [], []

    def handler(request):
        path = request.url.path
        if path == "/api/auth/me":
            return httpx.Response(200, json={"username": "op", "access": "chat"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path.endswith("/info"):
            return httpx.Response(200, json={"title": "t", "files": []})
        if path == "/api/chat":
            posted.append(json.loads(request.content))
            evs = [{"type": "start", "conversation_id": 4},
                   {"type": "ask_user", "id": "ask_1", "conversation_id": 4,
                    "questions": QS}, *events_extra,
                   {"type": "final", "content": "ok", "conversation_id": 4}]
            body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in evs)
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/chat/4/answer":
            answers.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "nope"})

    app = jav3.build_tui("http://h:1", "jvd_x", transport=httpx.MockTransport(handler))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        app.editor.text = "build it"
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.05)
            if type(app.screen).__name__ == "AskUser":
                break
        assert type(app.screen).__name__ == "AskUser"
        type(app.screen).GRACE = 0
        if check:
            check(type(app.screen))
        for k in keys:
            await pilot.press(k)
            await pilot.pause(0.02)
        for _ in range(60):
            await pilot.pause(0.05)
            if answers:
                break
    return answers


async def test_keys_pick_toggle_type_and_confirm():
    got = await _run(["2", "enter", "1", "3", "space", "down", "d", "o", "c", "s",
                      "enter"], check=check_markup)
    # "space" on row 3 (api, where 3 left the cursor) toggled it back off;
    # typing jumped to the free-text option
    assert got == [{"id": "ask_1", "answers": [
        {"selected": ["Postgres"], "text": None},
        {"selected": ["auth"], "text": "docs"}]}]


async def test_typing_replaces_single_pick_and_esc_skips():
    # esc on the second question warns that the first answer goes too (TUIB-13); again skips
    got = await _run(["1", "M", "y", "S", "Q", "L", "enter", "escape", "escape"])
    assert got == [{"id": "ask_1", "skipped": True}]


async def test_enter_takes_the_cursor_row():
    got = await _run(["down", "enter", "down", "down", "enter"])
    assert got == [{"id": "ask_1", "answers": [
        {"selected": ["Postgres"], "text": None},
        {"selected": ["api"], "text": None}]}]


async def test_shift_tab_cycles_permission_mode_and_permission_ask():
    posted, puts, answers = [], [], []
    always = "Yes, always allow this and similar commands (run_code: npm test)"

    def handler(request):
        path = request.url.path
        if path == "/api/auth/me":
            return httpx.Response(200, json={"username": "op", "access": "chat"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={"default": "deepseek/deepseek-flash",
                                             "models": [], "projects": [], "agents": []})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path.endswith("/info"):
            return httpx.Response(200, json={"title": "t", "files": []})
        if path == "/api/chat/4/permission_mode" and request.method == "PUT":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"mode": puts[-1]["mode"], "explicit": True})
        if path == "/api/chat":
            posted.append(json.loads(request.content))
            evs = [{"type": "start", "conversation_id": 4},
                   {"type": "ask_user", "id": "ask_p", "conversation_id": 4,
                    "kind": "permission", "tool": "run_code", "reason": "judged risky",
                    "detail": "npm test", "free_text_label": "No, tell the agent what to do instead",
                    "questions": [{"question": "Run in the VM: npm test",
                                   "options": ["Yes", always], "multi_select": False}]},
                   {"type": "final", "content": "ok", "conversation_id": 4}]
            body = "".join(f"data: {json.dumps(ev)}\n\n" for ev in evs)
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/event-stream"})
        if path == "/api/chat/4/answer":
            answers.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": "nope"})

    app = jav3.build_tui("http://h:1", "jvd_x", transport=httpx.MockTransport(handler))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app.perm_mode == "yolo"
        await pilot.press("shift+tab")
        await pilot.pause(0.05)
        assert app.perm_mode == "auto" and puts == []        # no chat yet: nothing saved
        app.editor.text = "test it"
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.05)
            if type(app.screen).__name__ == "AskUser":
                break
        scr = app.screen
        assert type(scr).__name__ == "AskUser"
        type(scr).GRACE = 0
        head, rows = scr.markup(scr.ev, scr.qs, 0, 0, set(), "", scr.free_label)
        assert "Permission" in head and "judged risky" in head and "npm test" in head
        assert "No, tell the agent" in rows
        await pilot.press("2")
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause(0.05)
            if answers:
                break
        await pilot.pause(0.2)
        await pilot.press("shift+tab")
        for _ in range(40):
            await pilot.pause(0.05)
            if puts:
                break
    assert posted[0]["permission_mode"] == "auto"
    assert answers == [{"id": "ask_p", "answers": [{"selected": [always], "text": None}]}]
    assert puts == [{"mode": "ask"}]
