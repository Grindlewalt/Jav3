"""A real HTTP server for the terminal client, over tests/cli_fake.py's FakeServer.

cli_fake.FakeServer answers httpx requests in-process (a MockTransport) and is what
the Textual-pilot tests use. scripts/tui_drive.py and tests/test_tui_snapshots.py run
the client as a separate process in a pty, so they need sockets. HttpFake puts a
stdlib asyncio HTTP/1.1 listener in front of any `handler(httpx.Request) ->
httpx.Response`; SeededServer is a FakeServer plus the routes the pages read
(chats, one running turn, boxes, security rows, agents). Unknown routes fall through
to FakeServer.handle, so the two fakes answer the shared routes the same way, and
every 404 is listed in `.misses` (a page that starts calling a new endpoint shows up
there instead of silently rendering empty).

Everything is a fixed function of CLOCK (a Unix time): seed timestamps are relative
to it, and the driver pins the client's time.time() to it, so a screen does not
depend on when it was taken. Writes (approve, restart, destroy ...) answer 200 and are
listed in `.writes`; the seeded rows do not change afterwards.

Chat turns: POST /api/chat plays a short scripted turn (search, read, edit, run, a
reply); a message containing "slow" runs a tool that never ends until the chat is
stopped, "error" ends in an error event, "ask" asks the operator a question and waits
for the answer. Chat #3 is already running: `jav3 -r 3` attaches to it.

Standalone, to point a client at it by hand:
    python tests/tui_fake_http.py [port]   (log in with the line it prints)
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_fake import FakeServer, Feed, call  # noqa: E402

CLOCK = 1790683200.0          # 2026-09-29 12:00:00 UTC (a Tuesday)
ZONE = "UTC"
USERNAME = "operator"
MODEL = "deepseek/deepseek-flash"
INTERRUPTED = "[Request interrupted by operator]"


def utc(offset_s: float = 0.0) -> str:
    """A server timestamp ('2026-09-29 11:35:00', naive UTC) `offset_s` from CLOCK."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(CLOCK + offset_s))


def iso(offset_s: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(CLOCK + offset_s))


# --- the HTTP listener ----------------------------------------------------------------

class HttpFake:
    """Serve `handler` on 127.0.0.1 from a background thread's event loop. The handler
    runs on that loop (a Feed's queue and `create_task` need it); the connection
    closes after each response."""

    def __init__(self, handler, port: int = 0) -> None:
        self.handler = handler
        # bound now, served from start(): the address is known (and a client may be
        # started, forking before there is a second thread) without the server running
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"

    def start(self) -> "HttpFake":
        self._thread = threading.Thread(target=self._run, name="tui-fake-http", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the fake HTTP server did not start")
        return self

    def stop(self) -> None:
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
            if self._thread:
                self._thread.join(5)
        self._sock.close()

    def __enter__(self) -> "HttpFake":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def boot():
            await asyncio.start_server(self._conn, sock=self._sock)
            self._ready.set()

        self.loop.run_until_complete(boot())
        try:
            self.loop.run_forever()
        finally:
            for t in asyncio.all_tasks(self.loop):
                t.cancel()
            self.loop.run_until_complete(asyncio.sleep(0))
            self.loop.close()

    async def _conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [(k.strip(), v.strip()) for k, v in
                       (ln.split(":", 1) for ln in lines[1:] if ":" in ln)]
            n = int(next((v for k, v in headers if k.lower() == "content-length"), 0))
            body = await reader.readexactly(n) if n else b""
            u = urlsplit(target)
            req = httpx.Request(method, f"http://{self.address}{u.path}"
                                + (f"?{u.query}" if u.query else ""),
                                headers=headers, content=body)
            resp = self.handler(req)
            skip = {"content-length", "transfer-encoding", "connection"}
            out = [f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}"]
            out += [f"{k}: {v}" for k, v in resp.headers.items() if k.lower() not in skip]
            try:
                data: bytes | None = resp.content
            except httpx.ResponseNotRead:
                data = None
            if data is not None:
                out += [f"Content-Length: {len(data)}", "Connection: close", "", ""]
                writer.write("\r\n".join(out).encode("latin-1") + data)
                await writer.drain()
            else:
                out += ["Transfer-Encoding: chunked", "Connection: close", "", ""]
                writer.write("\r\n".join(out).encode("latin-1"))
                await writer.drain()
                async for chunk in resp.aiter_raw():
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    await writer.drain()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:                  # a handler bug: say so rather than hang the client
            try:
                msg = json.dumps({"detail": f"fake server error: {type(e).__name__}: {e}"}).encode()
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json"
                             b"\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(msg) + msg)
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()


# --- seeded data --------------------------------------------------------------------------

PROJECTS = ["homelab", "website", "notes"]

REPLY = ("The flake was the backoff: `retry()` slept a fixed 0.1 s, so under load the third "
         "attempt raced the server's 0.2 s cool-down.\n\n"
         "- **src/sync/retry.py**: the delay now doubles on each attempt (0.1, 0.2, 0.4 s).\n"
         "- **tests/test_sync.py**: unchanged. It passed 50 runs in a row.\n\n"
         "Not checked: the copy of this helper in `src/backup/`, which has its own constants.")

SEARCH = call("search_codebase", {"query": "def retry", "subdir": "src"},
              "src/sync/retry.py:12: def retry(fn, attempts=3, delay=0.1):\n"
              "src/backup/util.py:40: def retry_copy(src, dst):")
READ = call("read_file", {"path": "src/sync/retry.py"},
            "def retry(fn, attempts=3, delay=0.1):\n    for i in range(attempts):\n"
            "        try:\n            return fn()\n        except OSError:\n"
            "            time.sleep(delay)\n    raise")
EDIT = call("edit_file", {"path": "src/sync/retry.py", "old_string": "time.sleep(delay)",
                          "new_string": "time.sleep(delay * 2 ** i)"},
            "edited src/sync/retry.py (1 replacement)")
RUN = call("run_code", {"command": "pytest tests/test_sync.py -q --count 50"},
           "exit 0 · 21.2s\n--- stdout ---\n50 passed in 20.9s")
FINISHED_TURN = [SEARCH, READ, EDIT, RUN]


def conversations() -> list[dict]:
    return [
        {"id": 4, "summary": "Fix the flaky retry test", "started_at": utc(-25 * 60),
         "starred": False, "project_slug": "homelab"},
        {"id": 3, "summary": "Benchmark the shadow pass", "started_at": utc(-8 * 60),
         "starred": False, "project_slug": "website"},
        {"id": 2, "summary": "Backup script for the NAS", "started_at": utc(-15 * 3600),
         "starred": False, "project_slug": "homelab"},
        {"id": 1, "summary": "Draft the release notes", "started_at": utc(-2 * 86400 + 7200),
         "starred": True, "project_slug": "notes"},
    ]


def messages_of(cid: int) -> dict:
    """GET /api/conversations/<id>/messages"""
    base = {"messages": [], "running": False, "pending_activity": [], "agent_slug": None}
    if cid == 4:
        base["messages"] = [
            {"id": 1, "role": "user", "created_at": utc(-25 * 60),
             "content": "tests/test_sync.py::test_retry fails about one run in five. Find out "
                        "why and fix it."},
            {"id": 2, "role": "assistant", "created_at": utc(-22 * 60), "model": MODEL,
             "content": REPLY, "activity": FINISHED_TURN}]
    elif cid == 3:
        base["messages"] = [
            {"id": 1, "role": "user", "created_at": utc(-8 * 60),
             "content": "Benchmark the cascaded shadow pass at 1, 2 and 4 cascades and tell "
                        "me which one to ship."}]
        base["running"] = True
        base["pending_activity"] = [
            call("read_file", {"path": "src/render/Shadows.js"}, "export class Shadows { ... }"),
            call("run_code", {"command": "npm run bench -- --cascades 1"},
                 "exit 0 · 38.0s\n--- stdout ---\nshadow pass: 1.9 ms/frame at 1 cascade")]
    elif cid == 2:
        base["messages"] = [
            {"id": 1, "role": "user", "created_at": utc(-15 * 3600),
             "content": "Write a backup script for the NAS."},
            {"id": 2, "role": "assistant", "created_at": utc(-15 * 3600 + 90), "model": MODEL,
             "content": "Done: `scripts/nas-backup.sh` rsyncs /srv to the second disk nightly.",
             "activity": [call("write_file", {"path": "scripts/nas-backup.sh",
                                              "content": "#!/bin/sh\nrsync -a /srv /mnt/b\n"},
                               "wrote scripts/nas-backup.sh")]}]
    return base


def info_of(cid: int) -> dict:
    """GET /api/conversations/<id>/info"""
    row = next((c for c in conversations() if c["id"] == cid), None) or {}
    base = {"title": row.get("summary") or f"Chat #{cid}", "project": row.get("project_slug"),
            "files": [], "local": None}
    if cid == 4:
        base.update(calls=4, input_tokens=18_400, output_tokens=1_250, cost_usd=0.0042,
                    context={"used": 21_000, "window": 1_000_000},
                    files=[{"path": "src/sync/retry.py", "writes": 1}])
    elif cid == 3:
        base.update(calls=6, input_tokens=61_000, output_tokens=2_300, cost_usd=0.0188,
                    context={"used": 64_000, "window": 1_000_000},
                    files=[{"path": "src/render/Shadows.js", "writes": 3},
                           {"path": "bench/shadows.mjs", "writes": 1}])
    return base


def agent_nodes(scope: str) -> tuple[list[dict], int]:
    if scope == "finished":
        nodes = [
            {"id": 4, "parent_id": None, "title": "Fix the flaky retry test", "kind": "chat",
             "project": "homelab", "model": MODEL, "status": "done", "running": False,
             "started_at": iso(-25 * 60), "ended_at": iso(-22 * 60)},
            {"id": 2, "parent_id": None, "title": "Backup script for the NAS", "kind": "chat",
             "project": "homelab", "model": MODEL, "status": "done", "running": False,
             "started_at": iso(-15 * 3600), "ended_at": iso(-15 * 3600 + 90)},
            {"id": 1, "parent_id": None, "title": "Draft the release notes", "kind": "chat",
             "project": "notes", "model": MODEL, "status": "failed", "running": False,
             "started_at": iso(-2 * 86400), "ended_at": iso(-2 * 86400 + 300)},
        ]
        return nodes, len(nodes)
    return [
        {"id": 3, "parent_id": None, "title": "Benchmark the shadow pass", "kind": "chat",
         "project": "website", "model": MODEL, "status": "running", "running": True,
         "started_at": iso(-8 * 60)},
        {"id": 7, "parent_id": 3, "title": "Profile the cascade passes", "kind": "agent",
         "agent_slug": "coder", "project": "website", "model": MODEL, "status": "running",
         "running": True, "started_at": iso(-5 * 60)},
        {"id": 8, "parent_id": 3, "title": "Screenshot the demo scene", "kind": "agent",
         "agent_slug": "browser", "project": "website", "model": MODEL, "status": "needs_you",
         "running": True, "needs": "run npm run bench in the VM?", "started_at": iso(-4 * 60)},
        {"id": 6, "parent_id": None, "title": "Release checklist", "kind": "orchestrator",
         "project": "notes", "model": MODEL, "status": "running", "running": True,
         "started_at": iso(-40 * 60)},
    ], 3


def boxes_payload() -> dict:
    common = {"disk": {}, "last_error": None, "inflight": 0, "now": [], "cid": None}
    shared = {**common, "id": "shared", "kind": "shared", "project": None, "projects": [],
              "cid": 3, "runtime": "kvm", "state": "running", "activity": "idle",
              "mem_mb": 768, "ram_cost_mb": 912, "image": {"variant": "main", "version": 4},
              "uptime_s": 312, "rss_bytes": 500_000_000, "cpu_pct": 4, "idle_s": 240,
              "stop_action": "scrub", "stop_after_s": 900, "stops_in_s": 660,
              "started_at": CLOCK - 312, "last_event": None, "net": {"tap": "jvtap0"}}
    homelab = {**common, "id": "p-homelab", "kind": "project", "project": "homelab",
               "projects": ["homelab"], "cid": 10, "runtime": "docker", "state": "running",
               "activity": "busy", "mem_mb": 512, "ram_cost_mb": 512, "inflight": 1,
               "image": {"variant": "main", "version": None}, "uptime_s": 900,
               "rss_bytes": 40_000_000, "cpu_pct": 31, "idle_s": None,
               "stop_action": "stop", "stop_after_s": 600, "stops_in_s": None,
               "started_at": CLOCK - 900, "last_event": None, "net": {"tap": "jvbr10"},
               "now": [{"op_id": "chat:4", "conversation_id": 4, "title": "Fix the flaky retry test",
                        "project": "homelab",
                        "tool": {"name": "run_code", "detail": "pytest tests/test_sync.py",
                                 "since": CLOCK - 20}}]}
    website = {**common, "id": "p-website", "kind": "project", "project": "website",
               "projects": ["website"], "cid": 11, "runtime": "kvm", "state": "stopped",
               "activity": "stopped", "mem_mb": 1024, "ram_cost_mb": 1024,
               "image": {"variant": "dev", "version": 2}, "uptime_s": None, "rss_bytes": None,
               "cpu_pct": None, "idle_s": None, "stop_action": "stop", "stop_after_s": 600,
               "stops_in_s": None, "started_at": None, "net": {"tap": "jvtap11"},
               "last_event": {"event": "idle_stopped", "created_at": utc(-3 * 3600)}}
    notes = {**common, "id": "p-notes", "kind": "project", "project": "notes",
             "projects": ["notes"], "cid": 12, "runtime": "kvm", "state": "stopped",
             "activity": "failed", "mem_mb": 512, "ram_cost_mb": 512,
             "image": {"variant": "main", "version": 4}, "uptime_s": None, "rss_bytes": None,
             "cpu_pct": None, "idle_s": None, "stop_action": "stop", "stop_after_s": 600,
             "stops_in_s": None, "started_at": None, "net": {"tap": "jvtap12"},
             "last_error": "qemu exited 1: could not open the disk image",
             "last_event": {"event": "crashed", "created_at": utc(-50 * 60)}}
    return {"enabled": True,
            "budget": {"ram_mb_used": 1280, "ram_mb_cap": 3072, "boxes": 4, "boxes_cap": 6,
                       "project_boxes": 3, "project_boxes_cap": 5},
            "idle": {"project_stop_s": 600, "shared_scrub_s": 900},
            "runtimes": {"kvm": {"available": True},
                         "docker": {"available": True, "weak": False, "warnings": []}},
            "boxes": [shared, homelab, website, notes]}


def images_payload() -> dict:
    return {"build": {"running": False},
            "variants": [
                {"name": "main", "from": "base", "builtin": True, "min_mem_mb": 512,
                 "used_by": ["homelab", "notes"], "recipe_sha256": "a1", "needs_build": False,
                 "versions": [
                     {"version": 4, "status": "built", "active": True, "size_bytes": 1_800_000_000,
                      "built_at": utc(-3 * 86400), "in_use_by": ["shared", "p-homelab"],
                      "recipe_sha256": "a1"},
                     {"version": 3, "status": "built", "size_bytes": 1_750_000_000,
                      "built_at": utc(-12 * 86400), "in_use_by": [], "recipe_sha256": "a0"}]},
                {"name": "dev", "from": "main", "min_mem_mb": 1024, "used_by": ["website"],
                 "recipe_sha256": "b2", "needs_build": True,
                 "versions": [
                     {"version": 2, "status": "built", "active": True, "size_bytes": 2_400_000_000,
                      "built_at": utc(-5 * 86400), "in_use_by": ["p-website"],
                      "recipe_sha256": "b1"}]}]}


def packages() -> list[dict]:
    return [
        {"id": 9, "manager": "pip", "package": "requests", "version_req": ">=2.31",
         "status": "pending", "project_slug": "homelab", "created_at": utc(-30 * 60),
         "target_variant": "main", "variant_used_by": ["homelab", "notes"]},
        {"id": 8, "manager": "apt", "package": "ffmpeg", "version_req": "", "status": "built",
         "resolved_version": "7.1", "project_slug": "website", "created_at": utc(-2 * 86400),
         "target_variant": "dev", "variant_used_by": ["website"]},
    ]


def alerts() -> list[dict]:
    return [
        {"id": 104, "kind": "egress_anomaly", "severity": "critical", "project_slug": "homelab",
         "summary": "volume spike to files.example.net (52428800 bytes)",
         "detail": {"host": "files.example.net", "bytes": 52428800}, "acknowledged": 0,
         "created_at": utc(-9 * 3600), "count": 1, "last_seen": utc(-9 * 3600),
         "tier": "critical"},
        {"id": 102, "kind": "unexpected_process", "severity": "warn", "project_slug": None,
         "summary": "Unexpected process in box shared: /usr/bin/odd", "detail": {"pid": 9},
         "acknowledged": 0, "created_at": utc(-26 * 3600), "count": 7,
         "last_seen": utc(-2 * 3600), "tier": "alert"},
        {"id": 101, "kind": "browser_session", "severity": "info", "project_slug": None,
         "summary": "browser 'Mac' connected for browser use", "detail": None,
         "acknowledged": 0, "created_at": utc(-27 * 3600), "count": 12,
         "last_seen": utc(-600), "tier": "record"},
        {"id": 99, "kind": "write_flag", "severity": "warn", "project_slug": "website",
         "summary": "write refused (secret pattern) in deploy.sh", "detail": None,
         "acknowledged": 1, "acknowledged_at": utc(-20 * 3600), "created_at": utc(-30 * 3600),
         "count": 1, "last_seen": utc(-30 * 3600), "tier": "alert"},
    ]


# --- the server --------------------------------------------------------------------------------

class SeededServer(FakeServer):
    """FakeServer, plus what every page of the TUI reads. See the module docstring."""

    def __init__(self) -> None:
        super().__init__(projects=PROJECTS, full=True)
        self.misses: list[str] = []
        self.writes: list[str] = []
        self.convs = conversations()
        self.next_cid = 5
        self._stops: dict[int, asyncio.Event] = {}
        self._answered: dict[str, asyncio.Event] = {}
        self.running = [3]

    # -- helpers -----------------------------------------------------------------

    def _stop_event(self, cid: int) -> asyncio.Event:
        return self._stops.setdefault(cid, asyncio.Event())

    @staticmethod
    def _json(data, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    @staticmethod
    def _sse(feed: Feed) -> httpx.Response:
        return httpx.Response(200, stream=feed, headers={"content-type": "text/event-stream"})

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks = [t for t in getattr(self, "_tasks", []) if not t.done()] + [task]

    # -- chat turns --------------------------------------------------------------

    async def _turn(self, feed: Feed, cid: int, text: str) -> None:
        """One scripted turn (events as backend/chat.py sends them)."""
        pause = 0.04
        scenario = ("slow" if "slow" in text else "error" if "error" in text
                    else "ask" if "ask" in text else "done")
        try:
            feed.put({"type": "start", "conversation_id": cid, "agent_slug": None,
                      "model": MODEL})
            await asyncio.sleep(pause)
            feed.put({"type": "token", "text": "I'll start with the retry test "})
            await asyncio.sleep(pause)
            feed.put({"type": "token", "text": "and the code it calls. "})
            if scenario == "slow":
                feed.put({"type": "tool", "id": "c1", "name": "run_code",
                          "args": {"command": "npm run bench -- --cascades 4"}})
                stop = self._stop_event(cid)
                await asyncio.wait_for(stop.wait(), 600)
                feed.put({"type": "final", "content": INTERRUPTED, "conversation_id": cid})
                return
            for i, c in enumerate(FINISHED_TURN[:2], 1):
                await asyncio.sleep(pause)
                feed.put({"type": "tool", "id": f"c{i}", "name": c["name"], "args": c["args"]})
                await asyncio.sleep(pause)
                feed.put({"type": "tool_result", "id": f"c{i}", "name": c["name"], "ok": True,
                          "result": c["result"]})
            if scenario == "error":
                await asyncio.sleep(pause)
                feed.put({"type": "error", "message": "the model provider answered 529 "
                                                      "(overloaded) three times; the turn was ended"})
                return
            if scenario == "ask":
                aid = f"ask_{cid}_1"
                self._answered[aid] = asyncio.Event()
                feed.put({"type": "ask_user", "id": aid, "conversation_id": cid,
                          "questions": [{"question": "Which retry policy should it use?",
                                         "options": ["Exponential backoff", "Fixed delay"],
                                         "multi_select": False}]})
                await asyncio.wait_for(self._answered[aid].wait(), 600)
                feed.put({"type": "ask_done", "id": aid, "conversation_id": cid})
            for i, c in enumerate(FINISHED_TURN[2:], 3):
                await asyncio.sleep(pause)
                feed.put({"type": "tool", "id": f"c{i}", "name": c["name"], "args": c["args"]})
                await asyncio.sleep(pause)
                feed.put({"type": "tool_result", "id": f"c{i}", "name": c["name"], "ok": True,
                          "result": c["result"]})
            for part in REPLY.split(" "):
                feed.put({"type": "token", "text": part + " "})
            await asyncio.sleep(pause)
            feed.put({"type": "final", "content": REPLY, "conversation_id": cid})
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            feed.close()
            if cid in self.running and cid != 3:
                self.running.remove(cid)

    async def _tail(self, feed: Feed, cid: int) -> None:
        """GET /api/chat/3/stream: the running turn is mid-way through a benchmark that
        only ends when the chat is stopped."""
        try:
            await asyncio.sleep(0.05)
            feed.put({"type": "tool", "id": "c3", "name": "run_code",
                      "args": {"command": "npm run bench -- --cascades 2"}})
            await asyncio.wait_for(self._stop_event(cid).wait(), 600)
            feed.put({"type": "final", "content": INTERRUPTED, "conversation_id": cid})
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            feed.close()

    # -- routing -----------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        resp = self.seeded(request)
        if resp is None:
            resp = super().handle(request)
            if resp.status_code == 404:
                if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                    self.writes.append(f"{request.method} {request.url.path}")
                    return self._json({"ok": True})
                self.misses.append(f"{request.method} {request.url.path}")
        return resp

    def seeded(self, request: httpx.Request) -> httpx.Response | None:
        path, method = request.url.path, request.method
        q = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        if self.down:
            return None
        J = self._json

        def logged(resp):
            self.calls.append((method, path))
            return resp

        if path in ("/api/devices/whoami", "/api/auth/me"):
            return logged(J({"username": USERNAME, "access": "full"}))
        if path == "/api/chat/options":
            return logged(J({
                "default": MODEL, "active_project": None,
                "models": [{"id": MODEL, "label": "DeepSeek Flash"},
                           {"id": "deepseek/deepseek-pro", "label": "DeepSeek Pro"}],
                "projects": [{"slug": s, "name": s} for s in PROJECTS], "agents": []}))
        if path == "/api/projects" and method == "GET":
            return logged(J({"projects": [{"slug": s, "name": s} for s in PROJECTS],
                             "active": None}))
        if path == "/api/conversations" and method == "GET":
            return logged(J({"conversations": self.convs}))
        if path == "/api/chat/running":
            return logged(J({"running": self.running}))
        if m := re.fullmatch(r"/api/conversations/(\d+)/messages", path):
            cid = int(m[1])
            if cid in self.conv_messages:
                return None
            return logged(J(messages_of(cid)))
        if m := re.fullmatch(r"/api/conversations/(\d+)/info", path):
            return logged(J(info_of(int(m[1]))))
        if path == "/api/chat" and method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            cid = body.get("conversation_id") or self.next_cid
            if not body.get("conversation_id"):
                self.next_cid += 1
                self.convs.insert(0, {"id": cid, "summary": body["message"][:40],
                                      "started_at": utc(), "starred": False,
                                      "project_slug": body.get("project")})
            self._stops.pop(cid, None)
            self.running.append(cid)
            feed = Feed()
            self.feeds.append(feed)
            self._spawn(self._turn(feed, cid, body["message"]))
            return logged(self._sse(feed))
        if m := re.fullmatch(r"/api/chat/(\d+)/stream", path):
            cid = int(m[1])
            if cid == 3 and 3 in self.running:
                feed = Feed()
                self._spawn(self._tail(feed, cid))
                return logged(self._sse(feed))
            return None
        if (m := re.fullmatch(r"/api/chat/(\d+)/stop", path)) and method == "POST":
            cid = int(m[1])
            self.stops.append(path)
            self._stop_event(cid).set()
            if cid in self.running:
                self.running.remove(cid)
            return logged(J({"stopped": True}))
        if path.endswith("/answer") and method == "POST":
            body = json.loads(request.content)
            self.answers.append(body)
            ev = self._answered.get(body.get("id"))
            if ev is not None:
                ev.set()
            return logged(J({"ok": True}))
        if path == "/api/chat/agents":
            nodes, total = agent_nodes(q.get("scope", "active"))
            return logged(J({"nodes": nodes, "total": total}))
        if path in ("/api/events", "/api/agents/notices/stream"):
            feed = Feed()
            feed.put({"topic": "security", "event": {"type": "stream_open"}}
                     if path == "/api/events" else {"type": "stream_open"})
            return logged(self._sse(feed))          # stays open, says nothing more
        # -- /security
        if path == "/api/egress/pending":
            return logged(J({"pending": [
                {"id": 11, "host": "registry.npmjs.org", "project_slug": "website",
                 "hit_count": 14, "first_seen": utc(-3600), "last_seen": utc(-120)},
                {"id": 12, "host": "files.example.net", "project_slug": "homelab", "hit_count": 2,
                 "first_seen": utc(-9 * 3600), "last_seen": utc(-9 * 3600 + 30),
                 "triage_verdict": "flag", "triage_reason": "unknown host serving archives"}]}))
        if path == "/api/security/events" and method == "GET":
            evs = alerts()
            if q.get("unacknowledged") == "true":
                evs = [e for e in evs if not e["acknowledged"]]
            return logged(J({"events": evs}))
        if path == "/api/services" and method == "GET":
            return logged(J({"services_lan_ip": "", "services": [
                {"id": 5, "name": "nas-sync", "command": ["python", "sync.py", "--watch"],
                 "project_slug": "homelab", "status": "pending", "created_at": utc(-900),
                 "ports": [{"port": 8088, "protocol": "tcp", "expose": "lan"}],
                 "placement": "per_project"},
                {"id": 4, "name": "metrics", "command": ["node", "metrics.js"],
                 "project_slug": "website", "status": "approved", "state": "running",
                 "placement": "per_service", "box_id": "svc-metrics", "created_at": utc(-86400),
                 "ports": [{"port": 9100, "protocol": "tcp", "expose": "none"}]}]}))
        if path == "/api/packages" and method == "GET":
            pk = packages()
            if q.get("status"):
                pk = [p for p in pk if p["status"] == q["status"]]
            return logged(J({"packages": pk}))
        if m := re.fullmatch(r"/api/projects/([\w-]+)/git/requests", path):
            reqs = [{"id": 3, "status": "pending", "kind": "commit", "created_at": utc(-600),
                     "message": "Back off exponentially in retry()",
                     "paths": ["src/sync/retry.py"]}] if m[1] == "homelab" else []
            return logged(J({"requests": reqs}))
        if path == "/api/egress/summary":
            return logged(J({"allowed": 212, "denied": 9, "waiting": 2}))
        if path == "/api/egress/events":
            return logged(J({"events": [
                {"id": 31, "project_slug": "website", "host": "registry.npmjs.org",
                 "method": "GET", "path": "/left-pad", "verdict": "allowed",
                 "bytes_out": 412, "bytes_in": 9_800, "created_at": utc(-300)},
                {"id": 30, "project_slug": "homelab", "host": "files.example.net",
                 "method": "GET", "path": "/dump.tar", "verdict": "waiting",
                 "bytes_out": 300, "bytes_in": 0, "created_at": utc(-9 * 3600)}]}))
        if re.fullmatch(r"/api/egress/policy/([\w-]+)", path) and method == "GET":
            return logged(J({"project_allow": ["pypi.org"], "project_deny": [],
                             "profile": {"name": "default", "default": "ask",
                                         "network_off": False}}))
        if path == "/api/profiles" and method == "GET":
            return logged(J({"profiles": [
                {"id": 1, "name": "default", "is_default": True, "box_runtime": "kvm",
                 "service_placement": "per_project", "default_verdict": "ask",
                 "network_off": False, "projects": ["homelab", "website"]},
                {"id": 2, "name": "offline", "box_runtime": "kvm", "service_placement": "shared",
                 "default_verdict": "deny", "network_off": True, "projects": ["notes"]}]}))
        if path == "/api/secrets" and method == "GET":
            return logged(J({"secrets": [{"name": "GITEA_TOKEN", "hosts": ["gitea.lan"]},
                                         {"name": "NPM_TOKEN", "hosts": []}]}))
        if path == "/api/vm/processes":
            return logged(J({"enabled": True, "boxes": []}))
        if path == "/api/permissions/rules":
            return logged(J({"rules": [{"id": 1, "tool": "run_code", "prefix": "pytest",
                                        "project_slug": "homelab",
                                        "created_at": utc(-86400)}]}))
        if path == "/api/logs/calls":
            return logged(J({"hours": 24, "rows": [
                {"id": 70, "ts": utc(-120), "kind": "call", "model": MODEL,
                 "conversation_id": 3, "project_slug": "website", "op_id": "chat:3",
                 "input_tokens": 61_000, "output_tokens": 800, "cache_hit": 52_000,
                 "cost_usd": 0.0061}]}))
        # -- /vms
        if path == "/api/vm/boxes" and method == "GET":
            return logged(J(boxes_payload()))
        if path == "/api/vm/leftovers":
            return logged(J({"items": [
                {"id": "l1", "name": "jvtap7", "type": "tap device", "cleanable": True,
                 "why": "no box owns it"}]}))
        if path == "/api/vm/images":
            return logged(J(images_payload()))
        if m := re.fullmatch(r"/api/vm/boxes/([\w-]+)/events", path):
            return logged(J({"events": [
                {"event": "started", "created_at": utc(-900), "actor": "chat:4"},
                {"event": "idle_stopped", "created_at": utc(-4 * 3600), "reason": "idle 11m",
                 "actor": "reaper"}]}))
        return None


if __name__ == "__main__":   # pragma: no cover
    srv = SeededServer()
    with HttpFake(srv.handle, int(sys.argv[1]) if len(sys.argv) > 1 else 0) as http:
        print(f"fake Jav3 server on http://{http.address} (session login; any password)",
              flush=True)
        threading.Event().wait()
