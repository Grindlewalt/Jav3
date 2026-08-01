"""Computer use: Jarvis drives the operator's desktop through a native client.

The existing play_music / play_movie / open_website tools act on the *browser
tab* (backend/gui.py pushes an event, the SPA renders a floating player). This
is the other thing: real OS-level control — the system mixer, whatever is
playing over MPRIS, a player on a chosen monitor and a chosen audio sink.

    tool -> backend/computeruse.py -> WebSocket -> clients/computeruse -> OS

THE SECURITY MODEL, which is the whole design:

There is no shell. Not "we're careful with quoting" — there is no code path
from a tool call to a command interpreter, and that is enforced structurally in
three independent places:

1.  The wire carries a VERB and typed PARAMS, never a command line. VERBS below
    is a closed table; the schema for each verb admits only enums, bounded
    integers, and strings that are themselves validated (a URL scheme, a path
    that must live under a granted root). There is no verb that takes a command,
    an argv array, or a format string, so there is nothing for an injection to
    inject into.

2.  The client re-validates everything against its own copy of this contract and
    refuses anything it does not recognise. It does not trust this backend. The
    design assumption for the whole project is that the agent may be compromised;
    a compromised Jarvis must not be able to widen what the client will do.

3.  The client re-checks containment on its own side. Every path it is handed is
    resolved and must sit inside a folder the operator granted, and that folder
    must really be a directory on that machine.

    This point used to say something stronger: the client's `--allow-root` flags
    were a ceiling that GUI grants could only narrow, so a hostile backend could
    only ever address files the operator had already pointed the client at. That
    was removed on the operator's instruction (2026-07-30). It made the Computer
    use tab dishonest — a folder granted there but absent from the launch flags
    looked accepted and reached nothing, and the only way to add one was to stop
    the client and re-run its set-up command. What is left is that grants are
    still made only by a logged-in operator in the GUI, never by a tool.

Everything the client executes is an absolute path resolved once at startup from
a fixed binary allowlist, spawned with an argv list and shell=False. See
clients/computeruse/agent.py — and tests/test_computeruse_noshell.py, which
fails the build if any of that stops being true.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets as _secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .db import get_db, get_state, set_state

# ---------------------------------------------------------------------------
# The verb contract. This table is the entire vocabulary; both sides import it.
# ---------------------------------------------------------------------------

# Media a granted folder may yield. Anything else is not playable and not worth
# the risk of handing an arbitrary file to a decoder.
# Kept in step with clients/computeruse/agent.py — a file the client will play
# and this host will not is a file that silently does not exist.
AUDIO_EXT = frozenset({".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
                       ".aac", ".wav", ".wma", ".aiff", ".aif", ".alac"})
VIDEO_EXT = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v",
                       ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".wmv",
                       ".flv", ".ogv", ".3gp"})
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT

# "output" moves the sound to another speaker. It sits with volume rather than
# in a verb of its own because it is the same decision from the operator's side —
# where the sound comes out — and the same privilege governs both.
VOLUME_ACTIONS = ("up", "down", "set", "mute", "unmute", "output")
TRANSPORT_ACTIONS = ("play", "pause", "playpause", "next", "previous", "stop")
MEDIA_KINDS = ("audio", "video")

# verb -> (description, param spec). The spec is deliberately dull: every field
# is an enum, a bounded int, a bool, or a string with its own validator.
VERBS: dict[str, dict] = {
    "status": {
        "doc": "Enumerate screens, audio devices and any MPRIS player.",
        "params": {},
    },
    "volume": {
        "doc": "System output volume.",
        "params": {
            "action": {"enum": VOLUME_ACTIONS, "required": True},
            "percent": {"int": (0, 100)},        # step for up/down, level for set
            "device": {"str": "device"},         # audio sink id from status
        },
    },
    "transport": {
        "doc": "Whatever is currently playing, over MPRIS.",
        "params": {
            "action": {"enum": TRANSPORT_ACTIONS, "required": True},
        },
    },
    "open_link": {
        "doc": "Open an http(s) URL in the operator's browser.",
        "params": {
            "url": {"str": "url", "required": True},
            "screen": {"int": (0, 15)},
        },
    },
    "play": {
        "doc": "Play a media file in the desktop player.",
        "params": {
            "kind": {"enum": MEDIA_KINDS, "required": True},
            "path": {"str": "path"},             # absolute, inside a granted root
            "url": {"str": "url"},               # or a stream (Jellyfin)
            "title": {"str": "text"},
            "screen": {"int": (0, 15)},
            "device": {"str": "device"},
            "volume": {"int": (0, 100)},
        },
    },
    "stop_playback": {
        "doc": "Stop the player this client started.",
        "params": {},
    },
    # These two are answered by the client from ITS filesystem. They used to be
    # done here, which only worked when the client happened to run on this same
    # host — never the case for the operator's laptop.
    "list": {
        "doc": "List one level of a granted folder on the client's disk.",
        "params": {
            "folder": {"str": "path_or_name"},
            "kind": {"enum": ("both", "audio", "video")},
            "limit": {"int": (1, 300)},
        },
    },
    "find": {
        "doc": "Search the granted folders on the client's disk.",
        "params": {
            "query": {"str": "text", "required": True},
            "kind": {"enum": ("both", "audio", "video")},
            "limit": {"int": (1, 100)},
        },
    },
}


class VerbError(ValueError):
    """A verb or its parameters failed the contract."""


def served_build_id() -> str:
    """Fingerprint of the client source THIS host hands out.

    The client computes the same digest over its own copy (agent.py:build_id)
    and reports it on connect. Different means that machine is running an older
    download — which has happened twice, because a CDN cached the tarball for
    hours and set-up kept installing a build that predated the fix being chased.
    A connected-but-stale client is indistinguishable from a broken one until
    something says so.
    """
    d = Path(__file__).resolve().parent.parent / "clients" / "computeruse"
    h = hashlib.sha256()
    try:
        for f in sorted(d.glob("*.py"), key=lambda p: p.name):
            h.update(f.name.encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    except OSError:
        return "unknown"
    return h.hexdigest()[:12]


def _check_url(value: str) -> str:
    """http(s) only. Everything else — file:, javascript:, smb:, data: — is a
    way to reach something that is not a web page, so none of them are allowed
    through however the URL was produced."""
    u = urlsplit(value)
    if u.scheme not in ("http", "https"):
        raise VerbError(f"only http(s) URLs may be opened, not {u.scheme or 'a bare string'!r}")
    if not u.hostname:
        raise VerbError("URL has no host")
    return value


def _check_device(value: str) -> str:
    """Audio device ids come back from `status` and go straight into an argv
    slot, so they are held to an identifier shape rather than trusted.

    '/' is allowed because mpv names its outputs "<ao>/<device>" —
    "pulse/alsa_output.pci-0000_00_1f.3.analog-stereo". It is not a shell
    metacharacter and nothing here reaches a shell, so the slash is safe; what
    matters is that spaces, quotes, semicolons and $ are not.
    """
    if len(value) > 200:
        raise VerbError("device id too long")
    # ',' and '=' are in real device names too:
    #   coreaudio/AppleHDAEngineOutput:1F,3,0,1:0
    #   alsa/default:CARD=PCH
    # None of / , = : + . _ - is a shell metacharacter, and nothing here reaches
    # a shell. What stays out is space, quotes, $ ` ; & | < > ( ) * ? ~ and
    # newlines.
    # Spaces and parentheses are allowed because this may also be a NAME to
    # match rather than an id -- "Built-in Audio Analog Stereo". The client
    # resolves whatever arrives against the devices it enumerated itself, so
    # the string that reaches argv is always one of ITS ids, never this value.
    # Safe regardless: argv is a list and shell=False, so a space is just a
    # space. What stays out is quotes, $ ` ; & | < > * ? ~ and control chars.
    if not all(c.isalnum() or c in "._:-+/,= ()[]" for c in value):
        raise VerbError(
            "device may only contain letters, digits, spaces and ._:-+/,=()[]")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise VerbError("device may not contain control characters")
    if value.strip() != value:
        raise VerbError("device may not start or end with whitespace")
    if value.startswith("-"):
        # it lands in an argv slot of its own, but a leading dash is how a value
        # gets read as a flag by whatever is being run
        raise VerbError("device id may not start with '-'")
    return value


def _check_text(value: str) -> str:
    if len(value) > 300:
        raise VerbError("text too long")
    # control characters are never meaningful in a title and are how a terminal
    # gets talked into doing something interesting
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise VerbError("text may not contain control characters")
    return value


def _check_path(value: str) -> str:
    """Shape only — containment inside a granted root is checked by
    resolve_local() below, and again by the client against its own roots."""
    if not value.startswith("/"):
        raise VerbError("path must be absolute")
    if "\x00" in value:
        raise VerbError("path contains a null byte")
    if len(value) > 4096:
        raise VerbError("path too long")
    return value


def _check_path_or_name(value: str) -> str:
    """A folder to open: either an absolute path or a plain subfolder name.
    Containment is the client's call, but '..' never has a legitimate use here
    and is refused on sight."""
    if "\x00" in value or len(value) > 4096:
        raise VerbError("bad folder")
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise VerbError("'..' is not allowed in a folder name")
    return value


_STR_CHECKS = {"url": _check_url, "device": _check_device,
               "text": _check_text, "path": _check_path,
               "path_or_name": _check_path_or_name}


def validate(verb: str, params: dict | None) -> dict:
    """Return the cleaned params for `verb`, or raise VerbError.

    Both the backend and the client call this. Unknown verbs and unknown
    parameters are refused rather than ignored: silently dropping a field a
    caller believed in is how a security control gets bypassed by accident.
    """
    spec = VERBS.get(verb)
    if spec is None:
        raise VerbError(f"unknown verb {verb!r} — allowed: {', '.join(sorted(VERBS))}")
    params = params or {}
    if not isinstance(params, dict):
        raise VerbError("params must be an object")
    allowed = spec["params"]
    unknown = set(params) - set(allowed)
    if unknown:
        raise VerbError(f"unknown parameter(s) for {verb}: {', '.join(sorted(unknown))}")

    clean: dict = {}
    for name, rule in allowed.items():
        if name not in params or params[name] is None:
            if rule.get("required"):
                raise VerbError(f"{verb} requires {name!r}")
            continue
        value = params[name]
        if "enum" in rule:
            if value not in rule["enum"]:
                raise VerbError(
                    f"{name} must be one of {', '.join(rule['enum'])}, not {value!r}")
            clean[name] = value
        elif "int" in rule:
            lo, hi = rule["int"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise VerbError(f"{name} must be a whole number")
            if not lo <= value <= hi:
                raise VerbError(f"{name} must be between {lo} and {hi}")
            clean[name] = value
        elif "str" in rule:
            if not isinstance(value, str):
                raise VerbError(f"{name} must be a string")
            clean[name] = _STR_CHECKS[rule["str"]](value)
        else:                                    # pragma: no cover - table bug
            raise VerbError(f"malformed spec for {name}")

    if verb == "play" and not ("path" in clean or "url" in clean):
        raise VerbError("play needs either a path or a url")
    if verb == "volume" and clean["action"] == "set" and "percent" not in clean:
        raise VerbError("volume set needs a percent")
    if verb == "volume" and clean["action"] == "output" and "device" not in clean:
        raise VerbError("volume output needs a device — name the speaker to "
                        "move the sound to (computer_status lists them)")
    return clean


# ---------------------------------------------------------------------------
# Folder grants: what of the operator's disk this may see.
# ---------------------------------------------------------------------------

# What a machine may be asked to do, in the operator's words rather than the
# protocol's. Verb names are an implementation detail; these are the decisions.
CAPABILITIES: dict[str, dict] = {
    "audio":     {"label": "Play music and audio",
                  "note": "from a granted folder, through this machine's speakers"},
    "video":     {"label": "Play video on a screen",
                  "note": "fullscreen on whichever monitor is asked for"},
    "volume":    {"label": "Change the volume and where sound comes out",
                  "note": "the system mixer, so it affects everything playing — "
                          "including moving it to another speaker"},
    "transport": {"label": "Pause and skip",
                  "note": "whatever is playing, including apps Jarvis did not start"},
    "links":     {"label": "Open links in the browser",
                  "note": "http and https only"},
    "browse":    {"label": "See what is in the granted folders",
                  "note": "names only, and only inside the folders below"},
}

# verb -> capability. status and stop_playback are absent on purpose: reading
# what a machine is, and stopping something Jarvis itself started, are not
# privileges worth revoking.
_VERB_CAP = {
    "volume": "volume",
    "transport": "transport",
    "open_link": "links",
    "list": "browse",
    "find": "browse",
}


def capability_for(verb: str, params: dict | None = None) -> str | None:
    """Which privilege a call needs, if any."""
    if verb == "play":
        return "video" if (params or {}).get("kind") == "video" else "audio"
    return _VERB_CAP.get(verb)


async def privileges(client: str) -> dict[str, bool]:
    """Everything this machine may do. Absent rows mean allowed — revoking is
    the deliberate act, so it is what gets recorded."""
    db = await get_db()
    try:
        async with db.execute(
                "SELECT capability, allowed FROM cu_privileges WHERE client = ?",
                (client,)) as cur:
            saved = {r["capability"]: bool(r["allowed"]) for r in await cur.fetchall()}
    except Exception:
        saved = {}
    finally:
        await db.close()
    return {cap: saved.get(cap, True) for cap in CAPABILITIES}


async def set_privilege(client: str, capability: str, allowed: bool) -> None:
    if capability not in CAPABILITIES:
        raise VerbError(f"unknown capability {capability!r}")
    if not (client or "").strip():
        raise VerbError("which machine?")
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO cu_privileges (client, capability, allowed) "
            "VALUES (?, ?, ?) ON CONFLICT(client, capability) DO UPDATE SET "
            "allowed = excluded.allowed, changed_at = datetime('now')",
            (client.strip(), capability, 1 if allowed else 0))
        await db.commit()
    finally:
        await db.close()


@dataclass
class Grant:
    id: int
    root: str
    label: str
    client: str = ""      # "" means every machine


async def list_grants(db=None, client: str | None = None) -> list[Grant]:
    """Every grant, or the ones that apply to one machine.

    A grant with no client applies everywhere — that is what the earlier
    global-only grants become. Filtering matters as soon as there are two
    machines: /Users/you/Movies exists on the Mac and nowhere else, and sending
    it to the Linux box just gives it a root it can never resolve.
    """
    own = db is None
    db = db or await get_db()
    try:
        async with db.execute(
                "SELECT id, root, label, client FROM cu_grants ORDER BY root") as cur:
            rows = [Grant(r["id"], r["root"], r["label"] or "", r["client"] or "")
                    for r in await cur.fetchall()]
    finally:
        if own:
            await db.close()
    if client is None:
        return rows
    want = client.strip().lower()
    return [g for g in rows if not g.client or g.client.lower() == want]


async def add_grant(root: str, label: str = "", client: str = "") -> Grant:
    """Grants are made here and only here — from the GUI, by the operator.

    There is deliberately no tool that creates one. If the agent could widen its
    own reach the grant would be decoration.
    """
    # No filesystem check here, deliberately. The folder lives on the
    # OPERATOR'S machine, not on this host: /Users/you/Movies does not exist on
    # the Pi, so is_dir() rejected every legitimate Mac and Windows grant and
    # made remote playback impossible. A grant is a declaration about a remote
    # path; the client is the only thing that can and does verify it, against
    # its own --allow-root ceiling and its own disk.
    raw = (root or "").strip()
    if raw.startswith("~"):
        # This host cannot expand a home directory that belongs to another
        # machine. Stored as "~/Movies" the grant matches nothing: the lexical
        # check here compares it against an absolute path and fails, and the
        # client resolves "~" against its working directory rather than $HOME.
        # It looks accepted and silently never works, so it is refused instead.
        raise VerbError(
            "use the full path rather than '~' — this host cannot know where "
            "home is on the machine being driven. On macOS that is usually "
            "/Users/<you>/... and on Linux /home/<you>/...")
    if not raw.startswith("/"):
        raise VerbError("grant root must be an absolute path")
    if "\x00" in raw:
        raise VerbError("path contains a null byte")
    # normpath only — lexical. resolve() would follow symlinks on THIS host,
    # which says nothing about the machine the path is actually on.
    p = Path(os.path.normpath(raw))
    db = await get_db()
    try:
        who = (client or "").strip() or None
        cur = await db.execute(
            "INSERT OR IGNORE INTO cu_grants (root, label, client) VALUES (?, ?, ?)",
            (str(p), label.strip()[:80], who))
        await db.commit()
        gid = cur.lastrowid
        if not gid:
            async with db.execute(
                    "SELECT id FROM cu_grants WHERE root = ? AND "
                    "(client IS ? OR client = ?)", (str(p), who, who or "")) as c2:
                row = await c2.fetchone()
                gid = row["id"] if row else 0
        return Grant(gid, str(p), label, who or "")
    finally:
        await db.close()


async def remove_grant(grant_id: int) -> None:
    db = await get_db()
    try:
        await db.execute("DELETE FROM cu_grants WHERE id = ?", (grant_id,))
        await db.commit()
    finally:
        await db.close()


async def path_within_grants(source: str, client: str | None = None) -> str:
    """A path the agent asked for -> an absolute path inside a granted folder.

    Lexical only. This host cannot see the operator's disk, so existence, file
    type and symlink resolution are all the client's job — it checks the real
    file against its own roots before playing anything. This is the early
    filter that gives a useful error without a round trip, not the control.
    """
    grants = await list_grants(client=client)
    if not grants:
        # A grant is scoped to a machine NAME, so a machine that reconnects
        # under a different name inherits none of them — and the old message
        # sent the operator to add folders they had already added. Name the
        # other machines' grants when there are some: that is the whole tell.
        others = {g.client for g in await list_grants() if g.client}
        elsewhere = ("" if not others else
                     f" There are folders granted to {', '.join(sorted(others))}"
                     f" — if that is the same computer under an older name, the"
                     f" grants need re-attaching to {client!r}.")
        raise VerbError(
            f"no folders are granted to {client or 'any machine'} — the "
            f"operator adds them on the Computer use tab; nothing on that "
            f"machine is reachable until they do.{elsewhere}")
    raw = (source or "").strip()
    if not raw:
        raise VerbError("empty source")
    if "\x00" in raw:
        raise VerbError("path contains a null byte")

    roots = [PurePosixPath(g.root) for g in grants]
    if raw.startswith("/"):
        cand = PurePosixPath(os.path.normpath(raw))
        for r in roots:
            if cand == r or r in cand.parents:
                return str(cand)
        raise VerbError(
            f"'{raw}' is not inside a granted folder. Granted: "
            + ", ".join(str(r) for r in roots))
    # relative: only unambiguous against a single root
    joined = [PurePosixPath(os.path.normpath(str(r / raw))) for r in roots]
    inside = [j for j, r in zip(joined, roots) if j == r or r in j.parents]
    if len(inside) == 1:
        return str(inside[0])
    if not inside:
        raise VerbError(f"'{raw}' escapes every granted folder")
    raise VerbError(
        f"'{raw}' is ambiguous across {len(inside)} granted folders — "
        "give the full path (computer_library shows them)")


# ---------------------------------------------------------------------------
# Jellyfin: the other place media can come from.
# ---------------------------------------------------------------------------
#
# The client never learns the API key. We resolve a search term to a stream URL
# here and hand over that URL, so the key stays on the Jarvis host — the same
# reason the egress proxy injects secrets rather than giving them to the guest.

async def jellyfin_config() -> tuple[str, str]:
    db = await get_db()
    try:
        return (await get_state(db, "cu_jellyfin_url") or "",
                await get_state(db, "cu_jellyfin_key") or "")
    except Exception:
        # unconfigured must read as unconfigured, not as a database error
        # quoted back at the model
        return "", ""
    finally:
        await db.close()


async def set_jellyfin_config(url: str, key: str) -> None:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise VerbError("Jellyfin URL must start with http:// or https://")
    db = await get_db()
    try:
        await set_state(db, "cu_jellyfin_url", url or None)
        # an empty key submission leaves the stored one alone, so the GUI can
        # show the URL without ever round-tripping the secret
        if key:
            await set_state(db, "cu_jellyfin_key", key)
        elif not url:
            await set_state(db, "cu_jellyfin_key", None)
    finally:
        await db.close()


async def jellyfin_find(query: str, kind: str = "audio", limit: int = 10) -> list[dict]:
    """Search the library. Returns [{id, name, artist, kind}]."""
    import httpx
    base, key = await jellyfin_config()
    if not base or not key:
        raise VerbError("Jellyfin is not configured — the operator adds the "
                        "server URL and an API key on the Computer use tab")
    want = "Audio" if kind == "audio" else "Movie,Episode,Video"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{base}/Items", params={
            "searchTerm": query, "IncludeItemTypes": want, "Recursive": "true",
            "Limit": limit, "api_key": key})
        r.raise_for_status()
        items = r.json().get("Items") or []
    return [{"id": i.get("Id"), "name": i.get("Name"),
             "artist": (i.get("AlbumArtist") or i.get("SeriesName") or ""),
             "kind": i.get("Type")} for i in items if i.get("Id")]


async def jellyfin_stream_url(item_id: str, kind: str = "audio") -> str:
    base, key = await jellyfin_config()
    if not base or not key:
        raise VerbError("Jellyfin is not configured")
    if not all(c.isalnum() or c == "-" for c in item_id):
        raise VerbError("bad Jellyfin item id")
    seg = "Audio" if kind == "audio" else "Videos"
    return f"{base}/{seg}/{item_id}/stream?static=true&api_key={key}"


# ---------------------------------------------------------------------------
# Connected clients.
# ---------------------------------------------------------------------------

@dataclass
class Client:
    """One connected desktop. `send` is filled in by the WebSocket endpoint."""
    id: str
    name: str
    platform: str
    caps: dict = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    send: object = None
    _waits: dict = field(default_factory=dict)

    def describe(self) -> dict:
        version = str(self.caps.get("version") or "")
        return {"id": self.id, "name": self.name, "platform": self.platform,
                "caps": self.caps, "connected_at": self.connected_at,
                "version": version,
                # unknown version = a build from before this was reported, which
                # is itself old enough to be worth saying out loud
                "stale": version != served_build_id()}


_clients: dict[str, Client] = {}


def register(client: Client) -> None:
    _clients[client.id] = client


def unregister(client_id: str) -> None:
    c = _clients.pop(client_id, None)
    if c:
        for fut in c._waits.values():
            if not fut.done():
                fut.set_exception(VerbError("client disconnected mid-command"))


def clients() -> list[Client]:
    return list(_clients.values())


def get_client(client_id: str | None) -> Client:
    """Find a connected machine by id, name, or an unambiguous prefix.

    Names are what the operator and the model actually use ("macbook"), while
    ids carry a random suffix so two machines called the same thing stay
    distinct. Matching accepts either, because expecting the model to quote
    "macbook-e17e0b" back is how it ends up guessing.
    """
    if not _clients:
        raise VerbError(
            "no computer-use client is connected — start the client on the "
            "machine you want driven (the Computer use tab has the command)")
    if client_id:
        want = client_id.strip().lower()
        if client_id in _clients:
            return _clients[client_id]
        by_name = [c for c in _clients.values() if c.name.lower() == want]
        if len(by_name) == 1:
            return by_name[0]
        if not by_name:
            by_name = [c for c in _clients.values()
                       if c.name.lower().startswith(want) or c.id.lower().startswith(want)]
        if len(by_name) == 1:
            return by_name[0]
        if not by_name:
            raise VerbError(
                f"no connected machine matches {client_id!r}. Connected: "
                + ", ".join(sorted(c.name for c in _clients.values())))
        raise VerbError(f"{client_id!r} matches several: "
                        + ", ".join(sorted(c.name for c in by_name)))
    if len(_clients) > 1:
        raise VerbError(
            "several machines are connected, so name one: "
            + ", ".join(sorted(c.name for c in _clients.values())))
    return next(iter(_clients.values()))


async def push_grants(client: Client) -> None:
    """Tell one connected machine which folders it may reach, now.

    Sent on connect and again on every change. Before this the list was only
    ever read at connect, so adding a folder in the GUI did nothing until the
    client was restarted — and restarting it meant re-running the set-up command
    on the operator's own laptop. Folders are the setting that changes most, so
    that was the whole tab's usefulness gated behind a terminal.

    Only this machine's folders: a path on the Mac is meaningless on the Linux
    box, and sending it just gives that client a root it can never resolve.
    """
    if client.send is None:
        return
    grants = [g.root for g in await list_grants(client=client.name)]
    try:
        await client.send(json.dumps({"grants": grants}))
    except Exception:
        # a machine that dropped mid-push gets the list on its next connect
        pass


async def broadcast_grants() -> None:
    """Push to every connected machine, each getting only its own folders."""
    for c in clients():
        await push_grants(c)


async def push_access_token(client: Client) -> bool:
    """Hand a connected machine the current Cloudflare Access service token.

    The rotation problem, solved for machines that are online. Jarvis sits
    behind Access, so a client whose token has gone stale cannot reach Jarvis to
    ask for the new one — but a client that is ALREADY connected has a working
    authenticated socket, and the new token can ride down it before the old one
    stops working. It saves it and uses it on its next reconnect, so nothing is
    interrupted.

    A machine that is offline through a rotation is beyond help by construction;
    it needs the new token pasted in once. That is the honest limit, and the
    tab says so rather than implying every machine is covered.
    """
    from . import cfaccess
    if client.send is None:
        return False
    cid, sec = cfaccess.get()
    if not (cid and sec):
        return False
    # Sent as a request and WAITED ON, rather than fired off. Reporting a
    # machine as updated because the bytes left the host would be a lie in the
    # one case that matters most: a client running an older build does not
    # understand this message, answers with an error, and would otherwise be
    # listed as current right up until it locked itself out on the next
    # reconnect. An error reply and a timeout both count as not updated.
    req_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._waits[req_id] = fut
    try:
        await client.send(json.dumps(
            {"id": req_id,
             "config": {"cf_access_id": cid, "cf_access_secret": sec}}))
        reply = await asyncio.wait_for(fut, 8.0)
        return bool(reply.get("ok"))
    except (asyncio.TimeoutError, Exception):
        return False
    finally:
        client._waits.pop(req_id, None)


async def broadcast_access_token() -> list[str]:
    """Push the token to every connected machine; return the ones that took it,
    so the operator is told which machines are now current and — by omission —
    which ones they will have to visit."""
    done = []
    for c in clients():
        if await push_access_token(c):
            done.append(c.name)
    return done


def resolve_result(client_id: str, req_id: str, payload: dict) -> None:
    c = _clients.get(client_id)
    if not c:
        return
    fut = c._waits.pop(req_id, None)
    if fut and not fut.done():
        fut.set_result(payload)


async def dispatch(verb: str, params: dict | None = None,
                   client_id: str | None = None, timeout: float = 20.0) -> dict:
    """Validate, send to the client, await its reply.

    Validation happens here so a bad tool call fails with a useful message
    before it ever reaches the operator's machine — but this is convenience, not
    the control. The client validates independently.
    """
    clean = validate(verb, params)
    c = get_client(client_id)
    # privilege check here rather than in each tool: this is the one place every
    # call passes through, so a revoked capability cannot be reached by a tool
    # that forgot to ask.
    cap = capability_for(verb, clean)
    if cap:
        allowed = await privileges(c.name)
        if not allowed.get(cap, True):
            raise VerbError(
                f"'{CAPABILITIES[cap]['label']}' is switched off for {c.name}. "
                f"The operator can turn it back on on the Computer use tab — do "
                f"not try another way round it.")
    req_id = uuid.uuid4().hex[:12]
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    c._waits[req_id] = fut
    try:
        await c.send(json.dumps({"id": req_id, "verb": verb, "params": clean}))
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        raise VerbError(f"{c.name} did not answer within {timeout:.0f}s")
    finally:
        c._waits.pop(req_id, None)


# ---------------------------------------------------------------------------
# Pairing.
# ---------------------------------------------------------------------------

async def pairing_token(rotate: bool = False) -> str:
    """The shared secret a client presents to connect. Shown on the Computer
    use tab; rotating it drops every client on their next reconnect."""
    db = await get_db()
    try:
        if not rotate:
            existing = await get_state(db, "cu_token")
            if existing:
                return existing
        tok = _secrets.token_urlsafe(32)
        await set_state(db, "cu_token", tok)
        return tok
    finally:
        await db.close()


async def check_token(presented: str) -> bool:
    return _secrets.compare_digest(presented or "", await pairing_token())
