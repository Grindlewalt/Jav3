"""The agent's seat at the box's desktop: jav3-desk's own Session on display :100.

display.py (`{"mode":"desk"}`) starts this as a child process of the display
listener and pipes the host's connection to its stdin/stdout. The frames are
the desk wire protocol (backend/desk.py), one JSON object per line:

    box -> host   hello, res, state, ceiling, ping      (what jav3-desk sends)
    host -> box   grants, req, kill, pong               (what the server sends)

A child, not a coroutine of the run-turn server, for three reasons: xdotool,
maim and the apps `open` launches need DISPLAY=:100 in THEIR environment (the
run-turn server's own commands must not inherit it), everything here runs
under memguard.confine so a runaway app costs the desktop and not the
run-turn server, and a crash in a capture tool ends one desk connection.

The client is shipped in the guest package as `backend/jav3_desk.py` (a copy of
clients/jav3-desk/jav3-desk, see backend/vm/guest_pkg.py): the same file that
runs on the operator's Mac, so a box desk and a computer behave alike.

`run(jd, backend, reader, writer)` is the testable core (a fake backend, no
X); `main()` wires the real one.
"""
import asyncio
import json
import os
import subprocess
import sys

APPS_PATH = os.environ.get("JAV3_DESK_APPS", "/run/jav3-desk-apps.json")
LINE_LIMIT = 1 << 20


class LineWS:
    """The transport Session.pump wants: `send(str)`, `close()` and async
    iteration over received text frames, as JSON lines on a pipe."""

    def __init__(self, reader: asyncio.StreamReader, writer):
        self._r, self._w = reader, writer

    async def send(self, text: str) -> None:
        self._w.write(text.encode() + b"\n")          # json.dumps never holds a raw newline
        await self._w.drain()

    async def close(self) -> None:
        try:
            self._w.close()
        except OSError:
            pass

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            try:
                line = await self._r.readline()
            except (ValueError, asyncio.LimitOverrunError):
                raise StopAsyncIteration
            if not line:
                raise StopAsyncIteration
            line = line.strip()
            if line:
                return line.decode(errors="replace")


def box_apps(path: str = APPS_PATH) -> dict[str, list[str]]:
    """name -> argv for `open app=`: what display.py wrote when the screen
    started (a terminal on tmux `desk`, a browser through the box's proxy)."""
    try:
        with open(path) as f:
            doc = json.load(f)
        return {k: v["argv"] for k, v in (doc.get("apps") or {}).items()
                if isinstance(k, str) and isinstance(v, dict)
                and isinstance(v.get("argv"), list) and v["argv"]}
    except (OSError, ValueError, AttributeError):
        return {}


def make_backend(jd, apps: dict[str, list[str]] | None = None):
    """jav3-desk's X11 backend for this box: nobody locks it and it never
    sleeps (no logind to ask), and `open url=` goes to the box's own browser."""
    browser = (apps or {}).get("browser")

    class BoxBackend(jd.X11Backend):
        name = "x11"

        def screen_state(self) -> dict:
            return {"locked": False, "asleep": False}

        def elements_source(self):
            return jd.ElementSource("the box's desktop has no accessibility tree")

        def open_url(self, url: str) -> None:
            if not browser:
                raise jd.DeskError("this desktop has no browser to open a URL in")
            subprocess.Popen([*browser, url], shell=False, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return BoxBackend()


async def run(jd, backend, reader: asyncio.StreamReader, writer,
              apps: dict[str, list[str]] | None = None) -> str:
    """One desk session over a pipe, until the host hangs up or kills it."""
    session = jd.Session(backend, "", "")
    if apps is not None:
        session.apps = dict(apps)
    ws = LineWS(reader, writer)
    try:
        return await session.pump(ws)
    finally:
        await ws.close()


async def _pipes():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    # the protocol rides the ORIGINAL stdout; anything else that prints goes to stderr
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    transport, proto = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, out)
    writer = asyncio.StreamWriter(transport, proto, None, loop)
    return reader, writer


def main() -> int:
    from . import jav3_desk as jd
    apps = box_apps()

    async def go():
        reader, writer = await _pipes()
        try:
            backend = make_backend(jd, apps)
        except jd.DeskError as e:       # e.g. xdotool missing: the host shows why
            writer.write((json.dumps({"type": "error", "error": str(e)}) + "\n").encode())
            await writer.drain()
            return "no backend"
        return await run(jd, backend, reader, writer, apps)

    try:
        asyncio.run(go())
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
