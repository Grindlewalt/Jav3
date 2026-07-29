#!/usr/bin/env python3
"""Jarvis computer-use client — runs on the operator's desktop.

    python3 agent.py --server https://jarvis.example --token <token> \
                     --allow-root ~/Music --allow-root ~/Videos

It dials OUT to Jarvis and waits for verbs. Nothing listens on this machine, so
there is no port to expose and closing the process ends all access.

WHY THERE IS NO SHELL HERE

Every command arrives as a verb name plus typed parameters, both checked against
VERBS below before anything happens. That table is the complete vocabulary, and
none of its entries carries a command, an argv array or a format string — there
is no field an injection could travel in.

What actually reaches the OS:

  * D-Bus method calls (MPRIS transport) — a library call, no process at all.
  * A small allowlist of binaries, each resolved to an absolute path once at
    startup by _find_binaries() and frozen. Nothing is ever looked up on PATH
    afterwards, so PATH cannot be moved under us mid-run.
  * Those binaries are spawned with an argv LIST and shell=False, in a scrubbed
    environment. subprocess never sees a string, so there is no interpreter and
    nothing to quote.

This module must not import os.system, use shell=True, or call eval/exec/compile.
tests/test_computeruse_noshell.py reads this file and fails if it does.

The --allow-root flags are the ceiling on what can be played. Jarvis sends folder
grants when we connect, but they are intersected with these — a grant naming a
folder outside them is dropped. A compromised server can therefore only address
files the operator already pointed this client at.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# --- the contract, kept in step with backend/computeruse.py -------------------

AUDIO_EXT = frozenset({".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
                       ".aac", ".wav", ".wma", ".aiff", ".alac"})
VIDEO_EXT = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"})

VOLUME_ACTIONS = ("up", "down", "set", "mute", "unmute")
TRANSPORT_ACTIONS = ("play", "pause", "playpause", "next", "previous", "stop")
MEDIA_KINDS = ("audio", "video")

# Binaries this client may ever run. Not a suggestion — _find_binaries resolves
# exactly these names and stores absolute paths; anything else is unreachable.
BINARIES = ("mpv", "pactl", "wpctl", "xdg-open", "xrandr",
            "osascript", "open")           # the last two are macOS


class Refused(Exception):
    """A command was rejected before it did anything."""


# --- parameter validation, independent of the server -------------------------

def _v_url(value):
    if not isinstance(value, str):
        raise Refused("url must be a string")
    u = urlsplit(value)
    if u.scheme not in ("http", "https"):
        raise Refused(f"refusing scheme {u.scheme!r}: only http(s) may be opened")
    if not u.hostname:
        raise Refused("url has no host")
    return value


_DEVICE_RE = re.compile(r"\A[A-Za-z0-9._:+-]{1,128}\Z")


def _v_device(value):
    if not isinstance(value, str) or not _DEVICE_RE.match(value):
        raise Refused("device id has an unacceptable shape")
    return value


def _v_text(value):
    if not isinstance(value, str) or len(value) > 300:
        raise Refused("text too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Refused("text may not contain control characters")
    return value


def _v_path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise Refused("path must be absolute and null-free")
    return value


def _v_int(lo, hi):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise Refused("expected a whole number")
        if not lo <= value <= hi:
            raise Refused(f"expected {lo}..{hi}")
        return value
    return check


def _v_enum(options):
    def check(value):
        if value not in options:
            raise Refused(f"expected one of {', '.join(options)}")
        return value
    return check


VERBS = {
    "status":        {},
    "volume":        {"action": (_v_enum(VOLUME_ACTIONS), True),
                      "percent": (_v_int(0, 100), False),
                      "device": (_v_device, False)},
    "transport":     {"action": (_v_enum(TRANSPORT_ACTIONS), True)},
    "open_link":     {"url": (_v_url, True), "screen": (_v_int(0, 15), False)},
    "play":          {"kind": (_v_enum(MEDIA_KINDS), True),
                      "path": (_v_path, False), "url": (_v_url, False),
                      "title": (_v_text, False), "screen": (_v_int(0, 15), False),
                      "device": (_v_device, False), "volume": (_v_int(0, 100), False)},
    "stop_playback": {},
}


def clean_params(verb, params):
    spec = VERBS.get(verb)
    if spec is None:
        raise Refused(f"unknown verb {verb!r}")
    params = params or {}
    if not isinstance(params, dict):
        raise Refused("params must be an object")
    extra = set(params) - set(spec)
    if extra:
        raise Refused(f"unknown parameter(s): {', '.join(sorted(extra))}")
    out = {}
    for name, (check, required) in spec.items():
        if name not in params or params[name] is None:
            if required:
                raise Refused(f"{verb} requires {name!r}")
            continue
        out[name] = check(params[name])
    if verb == "play" and "path" not in out and "url" not in out:
        raise Refused("play needs a path or a url")
    return out


# --- process launching -------------------------------------------------------

# A minimal environment. The child gets what it needs to find the display and
# the audio server and nothing else — no inherited LD_PRELOAD, no PATH games.
_ENV_KEEP = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
             "XAUTHORITY", "HOME", "USER", "LANG", "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS")


class Runner:
    """Everything that reaches the OS goes through here."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.bin = self._find_binaries()
        self.player = None                     # the mpv we started, if any

    @staticmethod
    def _find_binaries():
        """Resolve the allowlist once, to absolute paths. After this the client
        never consults PATH again."""
        found = {}
        for name in BINARIES:
            p = shutil.which(name)
            if p:
                real = os.path.realpath(p)
                if os.path.isfile(real) and os.access(real, os.X_OK):
                    found[name] = real
        return found

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
        env["PATH"] = "/usr/bin:/bin"           # for the child's own lookups
        return env

    def run(self, name, *args, capture=True, background=False):
        """Spawn an allowlisted binary with an argv list. Never a string, never
        a shell — `name` is a key into the frozen table, so it cannot be a path
        supplied by anyone."""
        exe = self.bin.get(name)
        if exe is None:
            raise Refused(f"{name} is not installed on this machine")
        argv = [exe, *[str(a) for a in args]]
        if self.dry_run:
            print(f"[dry-run] {argv}", flush=True)
            return ""
        if background:
            # shell=False is the default; stated because it is the invariant
            proc = subprocess.Popen(argv, shell=False, env=self._env(),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            return proc
        out = subprocess.run(argv, shell=False, env=self._env(),
                             stdin=subprocess.DEVNULL,
                             capture_output=capture, timeout=15, text=True)
        return (out.stdout or "") if capture else ""


# --- platform backends -------------------------------------------------------

class Linux:
    """PipeWire/PulseAudio for volume, MPRIS for transport, mpv for playback."""

    name = "linux"

    def __init__(self, runner):
        self.r = runner

    # -- audio ---------------------------------------------------------------
    def _mixer(self):
        if "wpctl" in self.r.bin:
            return "wpctl"
        if "pactl" in self.r.bin:
            return "pactl"
        raise Refused("neither wpctl nor pactl is installed — cannot reach the mixer")

    def volume(self, action, percent=None, device=None):
        m = self._mixer()
        sink = device or ("@DEFAULT_AUDIO_SINK@" if m == "wpctl" else "@DEFAULT_SINK@")
        step = percent if percent is not None else 5
        if m == "wpctl":
            if action == "up":
                self.r.run(m, "set-volume", sink, f"{step}%+")
            elif action == "down":
                self.r.run(m, "set-volume", sink, f"{step}%-")
            elif action == "set":
                self.r.run(m, "set-volume", sink, f"{step}%")
            elif action == "mute":
                self.r.run(m, "set-mute", sink, "1")
            else:
                self.r.run(m, "set-mute", sink, "0")
        else:
            if action == "up":
                self.r.run(m, "set-sink-volume", sink, f"+{step}%")
            elif action == "down":
                self.r.run(m, "set-sink-volume", sink, f"-{step}%")
            elif action == "set":
                self.r.run(m, "set-sink-volume", sink, f"{step}%")
            elif action == "mute":
                self.r.run(m, "set-sink-mute", sink, "1")
            else:
                self.r.run(m, "set-sink-mute", sink, "0")
        return {"ok": True, "action": action, "device": sink}

    def audio_devices(self):
        if "pactl" not in self.r.bin:
            return []
        try:
            raw = self.r.run("pactl", "-f", "json", "list", "sinks")
            return [{"id": s.get("name", ""),
                     "label": (s.get("description") or s.get("name") or "")}
                    for s in json.loads(raw or "[]")]
        except Exception:
            return []

    # -- transport (MPRIS over D-Bus: a library call, no process) -------------
    def transport(self, action):
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            raise Refused(
                "python3-jeepney is not installed — transport control needs it "
                "(pip install jeepney)")
        method = {"play": "Play", "pause": "Pause", "playpause": "PlayPause",
                  "next": "Next", "previous": "Previous", "stop": "Stop"}[action]
        with open_dbus_connection(bus="SESSION") as conn:
            names = conn.send_and_get_reply(new_method_call(
                DBusAddress("/org/freedesktop/DBus",
                            bus_name="org.freedesktop.DBus",
                            interface="org.freedesktop.DBus"), "ListNames"))
            players = [n for n in names.body[0]
                       if n.startswith("org.mpris.MediaPlayer2.")]
            if not players:
                raise Refused("nothing is playing (no MPRIS player on the bus)")
            for p in players:
                conn.send_and_get_reply(new_method_call(
                    DBusAddress("/org/mpris/MediaPlayer2", bus_name=p,
                                interface="org.mpris.MediaPlayer2.Player"), method))
            return {"ok": True, "action": action, "players": players}

    def players(self):
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.io.blocking import open_dbus_connection
            with open_dbus_connection(bus="SESSION") as conn:
                names = conn.send_and_get_reply(new_method_call(
                    DBusAddress("/org/freedesktop/DBus",
                                bus_name="org.freedesktop.DBus",
                                interface="org.freedesktop.DBus"), "ListNames"))
                return [n.rsplit(".", 1)[-1] for n in names.body[0]
                        if n.startswith("org.mpris.MediaPlayer2.")]
        except Exception:
            return []

    # -- screens -------------------------------------------------------------
    def screens(self):
        if "xrandr" not in self.r.bin:
            return []
        try:
            out = self.r.run("xrandr", "--listmonitors")
        except Exception:
            return []
        found = []
        for line in (out or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                found.append({"index": len(found), "id": parts[-1],
                              "geometry": parts[2]})
        return found

    # -- opening things ------------------------------------------------------
    def open_link(self, url, screen=None):
        if "xdg-open" not in self.r.bin:
            raise Refused("xdg-open is not installed")
        # url has already been checked to be http(s) with a host, twice
        self.r.run("xdg-open", url, background=True)
        note = "" if screen is None else (
            " (screen placement needs a window manager rule; opened on the "
            "browser's current screen)")
        return {"ok": True, "url": url, "note": note.strip() or None}

    def play(self, kind, path=None, url=None, title=None, screen=None,
             device=None, volume=None):
        if "mpv" not in self.r.bin:
            raise Refused("mpv is not installed — it is what plays media here")
        target = path or url
        args = [
            # --no-config and --load-scripts=no matter: mpv will otherwise read
            # ~/.config/mpv, and a Lua script there would be arbitrary code
            # running off the back of a play command.
            "--no-config", "--load-scripts=no", "--no-input-terminal",
            "--no-terminal", "--input-ipc-server=",
            "--msg-level=all=no",
        ]
        if kind == "audio":
            args.append("--no-video")
        else:
            args.append("--fullscreen")
            if screen is not None:
                args.append(f"--screen={screen}")
        if device is not None:
            args.append(f"--audio-device={device}")
        if volume is not None:
            args.append(f"--volume={volume}")
        if title:
            args.append(f"--force-media-title={title}")
        args.append("--")            # nothing after this is read as an option
        args.append(target)
        self.stop_playback()
        self.r.player = self.r.run("mpv", *args, background=True)
        return {"ok": True, "kind": kind, "playing": title or target}

    def stop_playback(self):
        p = self.r.player
        if p is not None and hasattr(p, "poll") and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        self.r.player = None
        return {"ok": True}


class MacOS(Linux):
    """macOS shares the verb surface; the mixer and opener differ.

    Volume and transport are not wired up yet — they need CoreAudio and
    MediaRemote rather than a CLI, and doing them through `osascript` would mean
    handing a scripting interpreter a string, which is exactly what this client
    exists to avoid. Playback and link opening work today.
    """

    name = "darwin"

    def volume(self, action, percent=None, device=None):
        raise Refused("volume control on macOS is not implemented yet")

    def transport(self, action):
        raise Refused("transport control on macOS is not implemented yet")

    def audio_devices(self):
        return []

    def screens(self):
        return []

    def open_link(self, url, screen=None):
        if "open" not in self.r.bin:
            raise Refused("/usr/bin/open is missing")
        self.r.run("open", url, background=True)
        return {"ok": True, "url": url}


# --- the client -------------------------------------------------------------

class Agent:
    def __init__(self, server, token, roots, name=None, dry_run=False):
        self.server = server.rstrip("/")
        self.token = token
        self.name = name or platform.node() or "desktop"
        self.dry_run = dry_run
        self.runner = Runner(dry_run=dry_run)
        self.os = MacOS(self.runner) if sys.platform == "darwin" else Linux(self.runner)
        # the ceiling: resolved once, never widened
        self.roots = []
        for r in roots:
            p = Path(r).expanduser().resolve()
            if p.is_dir():
                self.roots.append(p)
            else:
                print(f"! --allow-root {r} is not a directory; ignoring", flush=True)
        self.grants = list(self.roots)

    def set_grants(self, roots):
        """Narrow to the intersection of the server's grants and our ceiling."""
        kept = []
        for r in roots or []:
            try:
                p = Path(r).resolve()
            except OSError:
                continue
            if any(p == c or c in p.parents for c in self.roots):
                kept.append(p)
        self.grants = kept or list(self.roots)

    def check_playable(self, path, kind):
        """The client's own containment check. The backend did this too; doing
        it again here is the point — this side does not trust that one."""
        real = Path(path).resolve()
        if not any(real == g or g in real.parents for g in self.grants):
            raise Refused(f"{real} is outside every allowed root")
        if not real.is_file():
            raise Refused(f"{real} is not a file")
        want = AUDIO_EXT if kind == "audio" else VIDEO_EXT
        if real.suffix.lower() not in want:
            raise Refused(f"{real.suffix or 'no extension'} is not a {kind} type")
        return str(real)

    def handle(self, verb, params):
        p = clean_params(verb, params)
        if verb == "status":
            return {"ok": True, "platform": self.os.name,
                    "screens": self.os.screens(),
                    "audio_devices": self.os.audio_devices(),
                    "players": self.os.players(),
                    "roots": [str(g) for g in self.grants],
                    "dry_run": self.dry_run}
        if verb == "volume":
            return self.os.volume(p["action"], p.get("percent"), p.get("device"))
        if verb == "transport":
            return self.os.transport(p["action"])
        if verb == "open_link":
            return self.os.open_link(p["url"], p.get("screen"))
        if verb == "play":
            if "path" in p:
                p["path"] = self.check_playable(p["path"], p["kind"])
            return self.os.play(**p)
        if verb == "stop_playback":
            return self.os.stop_playback()
        raise Refused(f"unhandled verb {verb!r}")     # pragma: no cover

    async def serve(self):
        try:
            import websockets
        except ImportError:
            print("this client needs the 'websockets' package "
                  "(pip install websockets)", file=sys.stderr)
            return 2
        ws_url = (self.server.replace("https://", "wss://")
                             .replace("http://", "ws://")) + "/api/computeruse/agent"
        backoff = 1
        while True:
            try:
                async with websockets.connect(ws_url, max_size=64 * 1024) as ws:
                    await ws.send(json.dumps({
                        "token": self.token, "name": self.name,
                        "platform": self.os.name,
                        "caps": {"screens": self.os.screens(),
                                 "audio_devices": self.os.audio_devices(),
                                 "dry_run": self.dry_run},
                    }))
                    hello = json.loads(await ws.recv())
                    if not hello.get("ok"):
                        print(f"rejected: {hello.get('error')}", file=sys.stderr)
                        return 1
                    self.set_grants(hello.get("grants") or [])
                    print(f"connected to {self.server} as {self.name}; "
                          f"roots: {[str(g) for g in self.grants]}"
                          + (" [DRY RUN]" if self.dry_run else ""), flush=True)
                    backoff = 1
                    async for raw in ws:
                        await self._one(ws, raw)
            except Exception as e:
                print(f"disconnected ({e.__class__.__name__}: {e}); "
                      f"retrying in {backoff}s", file=sys.stderr, flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _one(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        req, verb = msg.get("id"), msg.get("verb")
        try:
            # the OS calls are blocking; keep the socket responsive
            result = await asyncio.get_running_loop().run_in_executor(
                None, self.handle, verb, msg.get("params"))
            payload = {"id": req, "ok": True, "result": result}
        except Refused as e:
            payload = {"id": req, "ok": False, "error": str(e)}
        except Exception as e:
            payload = {"id": req, "ok": False,
                       "error": f"{e.__class__.__name__}: {e}"}
        await ws.send(json.dumps(payload))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jarvis computer-use client")
    ap.add_argument("--server", required=True, help="https://host:port of Jarvis")
    ap.add_argument("--token", required=True, help="pairing token from the Computer use tab")
    ap.add_argument("--allow-root", action="append", default=[], metavar="DIR",
                    help="a folder Jarvis may play from. Repeatable. This is the "
                         "ceiling: grants made in the GUI can only narrow it.")
    ap.add_argument("--name", help="how this machine appears in Jarvis")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and log every action without running it")
    a = ap.parse_args(argv)
    agent = Agent(a.server, a.token, a.allow_root, a.name, a.dry_run)
    try:
        return asyncio.run(agent.serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
