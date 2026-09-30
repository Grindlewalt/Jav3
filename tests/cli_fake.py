"""Shared by the terminal-client dialog tests: load clients/jav3cli/jav3 and a fake
chat server whose event stream the test feeds by hand (so an ask can be answered
before the server says ask_done, the way the real one does)."""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import httpx

CLI = Path(__file__).resolve().parent.parent / "clients" / "jav3cli" / "jav3"


def load_client(name: str = "jav3cli_dialogs"):
    # JAV3_CLIENT: run the tests against another copy (the base commit's, to see them fail)
    path = os.environ.get("JAV3_CLIENT") or str(CLI)
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def pin_zone(monkeypatch, name: str = "UTC"):
    """Use as `yield from pin_zone(monkeypatch)` in an autouse fixture: the client shows
    the server's UTC times in the machine's zone, so a test that pins '14:03' pins a zone."""
    import time
    was = os.environ.get("TZ")
    monkeypatch.setenv("TZ", name)
    time.tzset()
    yield
    if was is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = was
    time.tzset()


class Feed(httpx.AsyncByteStream):
    """One open SSE response: put() events in, close() ends the stream, drop()
    breaks it the way a restarting server does."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    async def __aiter__(self):
        while True:
            item = await self.q.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def put(self, *events: dict) -> None:
        for ev in events:
            self.q.put_nowait(f"data: {json.dumps(ev)}\n\n".encode())

    def close(self) -> None:
        self.q.put_nowait(None)

    def drop(self, exc: Exception | None = None) -> None:
        self.q.put_nowait(exc or httpx.ReadError("[Errno 104] Connection reset by peer"))


class FakeServer:
    """Records what the client sends. Route results can be replaced per test:
    srv.projects, srv.post_status (POST /api/chat answers that status, no stream)."""

    def __init__(self, projects=(), full: bool = False) -> None:
        self.projects = list(projects)
        self.full = full
        self.feeds: list[Feed] = []
        self.posts: list[dict] = []
        self.answers: list[dict] = []
        self.stops: list[str] = []
        self.messages: list[dict] = []
        self.patches: list[dict] = []
        self.created: list[dict] = []
        self.post_status: int | None = None
        self.post_error: Exception | None = None
        self.answer_status = 200
        self.local_info: dict | None = None      # what /info says about a /local chat
        self.local_results: list[dict] = []
        self.perm_puts: list[dict] = []
        self.conv_messages: dict[int, dict] = {}   # cid -> /messages payload
        self.streams: dict[int, list[Feed]] = {}   # cid -> feeds handed to GET .../stream
        self.running: list[int] = []
        self.calls: list[tuple[str, str]] = []
        self.down = False           # every request fails to connect, like a restarting server

    @property
    def feed(self) -> Feed:
        return self.feeds[-1]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def drop(self) -> None:
        """The server goes away mid-turn: the open stream breaks and nothing answers."""
        self.down = True
        self.feed.drop()

    def back(self, cid: int, user: str, reply: str | None = None, activity=(),
             pending=(), running: bool = False) -> None:
        """The server answers again, holding what it saved of chat `cid` meanwhile:
        the user's message and, if the turn finished while the client was away, its
        reply with every call it made (`activity`); or, if it still runs, the calls so
        far (`pending`). A new GET .../stream then hands out a feed the test drives."""
        self.down = False
        msgs = [{"id": 1, "role": "user", "content": user, "created_at": "t0"}]
        if reply is not None:
            msgs.append({"id": 2, "role": "assistant", "content": reply, "created_at": "t1",
                         "model": "deepseek/deepseek-flash", "activity": list(activity)})
        self.conv_messages[cid] = {"messages": msgs, "running": running,
                                   "pending_activity": list(pending), "agent_slug": None}
        if running:
            self.running = [cid]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if self.down:
            raise httpx.ConnectError("All connection attempts failed")
        if path == "/api/devices/whoami":
            return httpx.Response(200, json={"username": "device:test"})
        if path == "/api/auth/me":
            return httpx.Response(200, json={"username": "op",
                                             "access": "full" if self.full else "chat"})
        if path == "/api/chat/options":
            return httpx.Response(200, json={
                "default": "deepseek/deepseek-flash", "active_project": None,
                "models": [{"id": "deepseek/deepseek-flash", "label": "Flash"}],
                "projects": [{"slug": s, "name": s} for s in self.projects], "agents": []})
        if path == "/api/projects" and method == "GET":
            return httpx.Response(200, json={"projects": [{"slug": s, "name": s}
                                                          for s in self.projects],
                                             "active": None})
        if path == "/api/projects" and method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            slug = body["name"].lower().replace(" ", "-")
            self.projects.append(slug)
            return httpx.Response(200, json={"slug": slug, "name": body["name"]})
        if path == "/api/conversations":
            return httpx.Response(200, json={"conversations": []})
        if path == "/api/chat/running":
            return httpx.Response(200, json={"running": self.running})
        if path.startswith("/api/conversations/") and path.endswith("/messages"):
            cid = int(path.split("/")[3])
            return httpx.Response(200, json=self.conv_messages.get(
                cid, {"messages": [], "running": False, "pending_activity": [],
                      "agent_slug": None}))
        if (path.startswith("/api/chat/") and path.endswith("/stream") and method == "GET"
                and path.split("/")[3].isdigit()):
            feed = Feed()
            self.streams.setdefault(int(path.split("/")[3]), []).append(feed)
            return httpx.Response(200, stream=feed, headers={"content-type": "text/event-stream"})
        if path.endswith("/info"):
            return httpx.Response(200, json={"title": "t", "files": [],
                                             "local": self.local_info})
        if path.startswith("/api/conversations/") and method == "PATCH":
            self.patches.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if path == "/api/chat" and method == "POST":
            self.posts.append(json.loads(request.content))
            if self.post_error is not None:
                raise self.post_error
            if self.post_status:
                return httpx.Response(self.post_status, json={"detail": "no such project: /project"})
            feed = Feed()
            self.feeds.append(feed)
            return httpx.Response(200, stream=feed,
                                  headers={"content-type": "text/event-stream"})
        if path.endswith("/local_result") and method == "POST":
            self.local_results.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/answer") and method == "POST":
            self.answers.append(json.loads(request.content))
            return httpx.Response(self.answer_status, json={"ok": self.answer_status == 200,
                                                            "detail": "not delivered"})
        if path.endswith("/stop") and method == "POST":
            self.stops.append(path)
            return httpx.Response(200, json={"stopped": True})
        if path.endswith("/message") and method == "POST":
            self.messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/permission_mode"):
            if method == "PUT":
                self.perm_puts.append(json.loads(request.content))
                return httpx.Response(200, json={"mode": self.perm_puts[-1]["mode"]})
            return httpx.Response(200, json={"mode": "yolo"})
        return httpx.Response(404, json={"detail": f"nope: {path}"})


def call(name: str, args: dict, result: str = "ok", ok: bool = True) -> dict:
    """One finished tool call the way GET /api/conversations/<id>/messages lists it."""
    return {"name": name, "args": args, "result": result, "ok": ok, "done": True}


async def wait_for(pred, tries: int = 80, step: float = 0.05):
    for _ in range(tries):
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


async def send(pilot, app, text: str) -> None:
    app.editor.text = text
    await pilot.press("enter")


def top(app) -> str:
    return type(app.screen).__name__


def ask(aid, agent_cid, question="Which one?", **extra):
    return {"type": "ask_user", "id": aid, "conversation_id": agent_cid,
            "questions": [{"question": question, "options": ["Yes", "No"],
                           "multi_select": False}], **extra}


async def open_chat(pilot, app, srv):
    """Send a first message and let the fake server start its turn (chat #4)."""
    await pilot.pause(0.3)
    await send(pilot, app, "go")
    assert await wait_for(lambda: srv.feeds)
    srv.feed.put({"type": "start", "conversation_id": 4})


def plain(app) -> str:
    """Everything the top dialog says, as text."""
    return "\n".join(str(w.render()) for w in app.screen.query("Static"))


def composer(app) -> str:
    return app.screen_stack[0].query_one("#editor").text


async def finish(srv, app, cid: int = 4) -> None:
    """End the open turn so the app can shut down cleanly."""
    srv.feed.put({"type": "final", "content": "ok", "conversation_id": cid})
    srv.feed.close()
    await wait_for(lambda: not app.busy)
