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
    got = await _run(["1", "M", "y", "S", "Q", "L", "enter", "escape"])
    assert got == [{"id": "ask_1", "skipped": True}]


async def test_enter_takes_the_cursor_row():
    got = await _run(["down", "enter", "down", "down", "enter"])
    assert got == [{"id": "ask_1", "answers": [
        {"selected": ["Postgres"], "text": None},
        {"selected": ["api"], "text": None}]}]
