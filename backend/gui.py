"""GUI control channel: backend -> every open browser tab.

Tools push small action events (open a URL, play media, layout changed) onto
one bus channel; each logged-in tab holds an SSE subscription (/api/gui/stream)
and reacts. Events are fire-and-forget views onto the operator's screen — the
only durable thing here is the workspace layout, which edits the same
.workspace.json the board itself saves, so a change shows up live in an open
tab AND on the next visit.
"""
import asyncio
import json
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from . import bus
from .auth import require_user
from .config import settings
from .fsutil import safe_join

GUI_CHAN = "gui"


# --- who is listening ---------------------------------------------------------
#
# Every open Jarvis tab used to be one anonymous subscriber, and a push went to
# all of them. For a toast that is right. For MUSIC it is not: asking for a song
# started it on the laptop, the desktop and the phone at once, all slightly out
# of sync, and there was no way to say which one you meant because none of them
# had a name.
#
# So a tab identifies itself when it subscribes: an id it keeps for its lifetime
# and a name the operator can read ("Mac · Chrome"). A push can then be addressed
# to one of them. Deliberately in-memory — this describes browser tabs that exist
# right now, and it is worthless the moment the process restarts.

_tabs: dict[str, dict] = {}


def register_tab(tab_id: str, name: str, queue) -> None:
    _tabs[tab_id] = {"id": tab_id, "name": (name or "").strip()[:60] or "a tab",
                     "queue": queue, "opened_at": time.time(),
                     "last_seen": time.time()}


def forget_tab(tab_id: str) -> None:
    _tabs.pop(tab_id, None)


def touch_tab(tab_id: str) -> None:
    """Mark a tab as the one being used. Called when a chat turn arrives from
    it, which is the only signal the host gets that somebody is actually sitting
    in front of that tab."""
    t = _tabs.get(tab_id)
    if t:
        t["last_seen"] = time.time()


def tab_list() -> list[dict]:
    """Open tabs, most recently used first."""
    return [{k: v for k, v in t.items() if k != "queue"}
            for t in sorted(_tabs.values(), key=lambda t: -t["last_seen"])]


def resolve_tab(want: str | None, asked_from: str | None = None) -> tuple[str | None, str]:
    """Which tab a tool should play in -> (tab id or None, explanation).

    The order is the whole point of this change:

      1. a tab the model named, matched on name or id
      2. the tab the request came from — "play something" said in a chat on the
         Mac means on the Mac, and that is what the operator asked for
      3. the only open tab, when there is exactly one
      4. the most recently used one, for a scheduled or headless run where
         there is no asking tab at all

    None means there is nothing open to play in, and the caller must say so
    rather than falling back to every tab — falling back to every tab is the
    behaviour being removed.
    """
    if want:
        w = want.strip().lower()
        hit = [t for t in _tabs.values()
               if t["id"] == want or t["name"].lower() == w]
        if not hit:
            hit = [t for t in _tabs.values() if w in t["name"].lower()]
        if len(hit) == 1:
            return hit[0]["id"], hit[0]["name"]
        if not hit:
            open_now = ", ".join(t["name"] for t in tab_list()) or "none"
            return None, f"no open Jarvis tab matches {want!r} (open: {open_now})"
        return None, (f"{want!r} matches several open tabs: "
                      + ", ".join(t["name"] for t in hit))
    if asked_from and asked_from in _tabs:
        return asked_from, _tabs[asked_from]["name"]
    if len(_tabs) == 1:
        only = next(iter(_tabs.values()))
        return only["id"], only["name"]
    order = tab_list()
    if order:
        return order[0]["id"], order[0]["name"]
    if bus.subscriber_count(GUI_CHAN):
        # Somebody is listening but nobody said who they are — a page still
        # running the bundle from before tabs had names, left open across the
        # deploy that added them. Refusing here would make music silently stop
        # working until every browser was reloaded, so this is the one case
        # where the old behaviour is still the right answer. "" means broadcast.
        return "", "every open tab (an older Jarvis page — reload it to name it)"
    return None, "no Jarvis tab is open"


def push(event: dict, tab: str | None = None) -> int:
    """Publish an action to every connected tab, or to one named tab.

    Returns how many tabs will see it, so a tool can tell the model honestly
    whether anything saw it. A `tab` that has since closed returns 0 rather than
    quietly going to all of them — "" is the deliberate broadcast (see
    resolve_tab), None is "not addressed to anyone in particular".
    """
    if tab:
        t = _tabs.get(tab)
        if not t:
            return 0
        bus.publish_to(t["queue"], event)
        return 1
    n = bus.subscriber_count(GUI_CHAN)
    if n:
        bus.publish(GUI_CHAN, event)
    return n


def tabs() -> int:
    """How many browser tabs are listening. Lets a tool decide whether the
    in-page player is even a possible destination before choosing one."""
    return bus.subscriber_count(GUI_CHAN)


# --- the in-page music player -------------------------------------------------
# The player is an <audio> element in the operator's browser, so the host cannot
# observe it — the tab reports instead. Deliberately a process global and not a
# table: it describes a browser tab that exists right now, exactly like TARMAC's
# own `playerState`, and it is worthless the moment the process restarts.
#
# `started` is the load-bearing field. A browser refuses audio.play() in a tab
# that has had no user gesture, and TARMAC returns ok for the BROADCAST rather
# than for any sound, so "accepted" and "audible" are different facts. The tab
# sets started only once play() actually resolves, which is what lets music_play
# tell the truth instead of claiming music is playing into silence.

_player: dict = {
    "track": None,      # {id, title, artist, album, duration}
    "paused": True,
    "position": 0.0,
    "duration": None,
    "queue": 0,         # tracks left after this one
    "volume": 100,
    "started": False,
    "error": "",
    "reported_at": None,
    # which tab is doing the reporting. Only one tab is ever addressed at a
    # time, but a second tab left playing from earlier would otherwise overwrite
    # this and make the confirmation describe the wrong machine.
    "tab": "",
}


def player_report(state: dict) -> dict:
    """The tab telling the host what it is actually doing."""
    _player.update({k: v for k, v in state.items() if k in _player})
    _player["reported_at"] = time.time()
    return dict(_player)


def player_status() -> dict:
    """What the host believes the in-page player is doing. `stale` is honest
    about the tab having gone away without saying goodbye — a closed laptop
    leaves the last report sitting here forever otherwise."""
    out = dict(_player)
    at = out.get("reported_at")
    out["stale"] = at is None or (time.time() - at) > 30
    out["tabs"] = tabs()
    return out


def player_push(action: str, tab: str | None = None, **fields) -> int:
    """Drive the in-page player in ONE tab. Returns 1, or 0 if that tab is gone.

    `tab` used to not exist and every open tab started the same track — the
    laptop, the desktop and the phone all at once. Callers resolve the tab with
    resolve_tab() and pass it here.
    """
    if action == "play":
        _player.update({"started": False, "error": "", "paused": False,
                        "tab": tab or ""})
    return push({"type": "player", "action": action, **fields}, tab=tab)


def stream_url(track_id) -> str:
    """Where the browser fetches a library track — Jarvis's own origin, because
    TARMAC's Cloudflare Access application is a different one."""
    return f"/api/computeruse/tarmac/stream/{int(track_id)}"


# --- media source resolution --------------------------------------------------

def media_src(source: str, slug: str | None) -> tuple[str | None, str | None]:
    """(src, error): a project-relative path becomes a same-origin raw URL; an
    http(s) URL must sit on the operator's media allowlist (the same
    `media_hosts` config the chat renderer enforces, so the GUI's CSP will
    actually let it load)."""
    s = (source or "").strip()
    if not s:
        return None, "empty source"
    if s.startswith(("http://", "https://")):
        host = (urlsplit(s).hostname or "").lower()
        allowed = [h.lower() for h in settings.media_hosts]
        if not any(host == h or host.endswith("." + h) for h in allowed):
            return None, (
                f"host '{host}' is not on the media allowlist "
                f"({', '.join(allowed) or 'empty'}) — the browser would block it. "
                "The operator can extend JARVIS_MEDIA_HOSTS in the config.")
        return s, None
    if not slug:
        return None, ("no active project to resolve that file path against — "
                      "load a project first or pass a full URL")
    try:
        p = safe_join(settings.projects_dir / slug, s)
    except Exception:
        return None, f"path '{s}' escapes the project directory"
    if not p.is_file():
        return None, f"no such file in project '{slug}': {s}"
    encoded = "/".join(part for part in s.split("/"))
    return f"/api/projects/{slug}/raw/{encoded}", None


# --- workspace layout ---------------------------------------------------------
# Mirrors the GUI's PANEL_TYPES / DEFAULT_PANELS (Workspace.jsx). If a panel
# type is added there, add it here or workspace_panel refuses it.

PANEL_SIZES = {
    "chat": (440, 520), "journal": (460, 420), "editor": (520, 440),
    "renderer": (520, 440), "organizer": (580, 460), "run": (560, 470),
    "todos": (360, 380), "git": (560, 480), "board": (400, 540),
    "context": (440, 460), "agent": (460, 520), "research": (620, 560),
    "review": (480, 540), "network": (480, 560), "secrets": (460, 380),
    "terminal": (560, 360),
}

DEFAULT_PANELS = [
    {"id": "p1", "type": "chat", "x": 16, "y": 16, "w": 460, "h": 560, "z": 1, "state": {}},
    {"id": "p2", "type": "board", "x": 492, "y": 16, "w": 400, "h": 560, "z": 2, "state": {}},
    {"id": "p3", "type": "git", "x": 908, "y": 16, "w": 540, "h": 300, "z": 3, "state": {}},
    {"id": "p4", "type": "network", "x": 908, "y": 332, "w": 540, "h": 244, "z": 4, "state": {}},
]

GAP = 12
ROW_WIDTH = 1420      # tile() wraps rows at roughly a laptop-wide board


def _layout_path(slug: str):
    return settings.projects_dir / slug / ".workspace.json"


def load_panels(slug: str) -> list[dict]:
    """The saved board, or the same defaults the GUI would show for a fresh
    project — so a tool edit on a never-arranged board doesn't wipe the
    default panels the operator sees."""
    p = _layout_path(slug)
    if p.exists():
        try:
            panels = json.loads(p.read_text()).get("panels") or []
            if panels:
                return panels
        except (json.JSONDecodeError, OSError):
            pass
    return [dict(p, state=dict(p["state"])) for p in DEFAULT_PANELS]


def save_panels(slug: str, panels: list[dict]) -> int:
    _layout_path(slug).write_text(json.dumps({"panels": panels}))
    return push({"type": "layout_changed", "slug": slug})


def add_panel(panels: list[dict], ptype: str, state: dict | None = None) -> dict:
    """Append a panel to the right of the board; returns the new panel."""
    w, h = PANEL_SIZES[ptype]
    nums = [int(str(p.get("id"))[1:]) for p in panels
            if str(p.get("id", ""))[1:].isdigit()]
    panel = {
        "id": f"p{max(nums or [0]) + 1}",
        "type": ptype,
        "x": max([16] + [p["x"] + p["w"] + GAP for p in panels]),
        "y": 16, "w": w, "h": h,
        "z": max([0] + [p.get("z", 0) for p in panels]) + 1,
        "state": state or {},
    }
    panels.append(panel)
    return panel


def remove_panels(panels: list[dict], ptype: str) -> tuple[list[dict], int]:
    kept = [p for p in panels if p["type"] != ptype]
    return kept, len(panels) - len(kept)


def tile_panels(panels: list[dict]) -> list[dict]:
    """Shelf-pack every panel in reading order, keeping each panel's size."""
    out, x, y, shelf_h = [], 16, 16, 0
    for p in sorted(panels, key=lambda p: (p["y"], p["x"])):
        if x > 16 and x + p["w"] > ROW_WIDTH:
            x, y, shelf_h = 16, y + shelf_h + GAP, 0
        out.append({**p, "x": x, "y": y})
        x += p["w"] + GAP
        shelf_h = max(shelf_h, p["h"])
    return out


# --- SSE endpoint -------------------------------------------------------------

router = APIRouter(prefix="/api/gui", tags=["gui"],
                   dependencies=[Depends(require_user)])


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@router.get("/stream")
async def gui_stream(tab: str = "", name: str = ""):
    """One tab's subscription. `tab` is an id the tab keeps for its lifetime and
    `name` is what the operator would call that machine — both optional, so an
    older SPA still gets the broadcast behaviour it expects."""
    queue = bus.subscribe(GUI_CHAN)
    tab_id = (tab or "").strip()[:64]
    if tab_id:
        register_tab(tab_id, name, queue)

    async def gen():
        try:
            yield _sse({"type": "stream_open", "channel": GUI_CHAN,
                        "tab": tab_id})
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=25)
                    yield _sse(ev)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(GUI_CHAN, queue)
            if tab_id:
                forget_tab(tab_id)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/tabs")
async def gui_tabs():
    """Which Jarvis tabs are open, for the Computer use tab to show and rename."""
    return {"tabs": tab_list()}
