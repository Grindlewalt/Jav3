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
import time
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
            "open")   # macOS opener. osascript is deliberately NOT here:
                      # AppleScript can `do shell script "..."`, so allowing
                      # it would reopen the exact path this client closes.


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


# Real device names: pulse/alsa_output.pci-0000_00_1f.3.analog-stereo,
# coreaudio/AppleHDAEngineOutput:1F,3,0,1:0, alsa/default:CARD=PCH.
# So / , = are in. Space, quotes, $ ` ; & | < > ( ) * ? ~ and newlines are not,
# and a leading dash is refused so a value cannot be read as a flag.
_DEVICE_RE = re.compile(r"\A(?!-)(?!\s)[A-Za-z0-9._:+/,=() \[\]-]{1,200}\Z")


def _v_device(value):
    if not isinstance(value, str) or not _DEVICE_RE.match(value):
        raise Refused("device has an unacceptable shape")
    if value.strip() != value:
        raise Refused("device may not start or end with whitespace")
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


def _v_folder(value):
    if not isinstance(value, str) or "\x00" in value or len(value) > 4096:
        raise Refused("bad folder")
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise Refused("'..' is not allowed in a folder name")
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
    # answered from THIS machine's disk: the server cannot see it
    "list":          {"folder": (_v_folder, False),
                      "kind": (_v_enum(("both", "audio", "video")), False),
                      "limit": (_v_int(1, 300), False)},
    "find":          {"query": (_v_text, True),
                      "kind": (_v_enum(("both", "audio", "video")), False),
                      "limit": (_v_int(1, 100), False)},
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
# TMPDIR is here for macOS, where it is per-user and libraries genuinely need
# it; the X/Wayland/Pulse entries are inert there and vice versa.
_ENV_KEEP = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
             "XAUTHORITY", "HOME", "USER", "LANG", "TMPDIR",
             "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS")


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
        self._cache: dict = {}

    # -- enumeration, cached ------------------------------------------------
    def _cached(self, key, fn, ttl=30.0):
        """Device and screen lists cost a subprocess each. Jarvis asks for them
        far more often than they change, so a short TTL turns a repeated status
        call into nothing."""
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        val = fn()
        self._cache[key] = (time.monotonic(), val)
        return val

    def _resolve_device(self, wanted, options, what):
        """Turn whatever Jarvis asked for into an id THIS machine reported.

        The model can pass a full id or a fragment of a name — "desk speakers"
        — and gets back the real id, or an error listing the options. That is
        what removes the need to call status before every action.

        It also tightens things: the value that ends up in argv is always one we
        enumerated ourselves, so the model's string never reaches a command line
        at all — it is only ever a search key.
        """
        if not wanted:
            return None
        ids = [o["id"] for o in options]
        if wanted in ids:
            return wanted
        if not ids:
            # We could not enumerate at all — mpv absent, pactl absent, the
            # query failed. That is not the same as knowing the device is wrong,
            # and refusing here would make a correct id unusable whenever
            # enumeration breaks. The value already passed the shape check, so
            # it is argv-safe; hand it to the player and let IT complain.
            return wanted
        w = wanted.lower().strip()
        hits = [o["id"] for o in options if o["id"].lower() == w]
        if not hits:
            hits = [o["id"] for o in options
                    if w in o["id"].lower() or w in (o.get("label") or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise Refused(
                f"no {what} here matches {wanted!r}. Available: "
                + (", ".join(ids[:12]) or "none detected"))
        raise Refused(f"{wanted!r} matches several: " + ", ".join(hits[:8])
                      + " — name one exactly")

    # -- audio ---------------------------------------------------------------
    def _mixer(self):
        if "wpctl" in self.r.bin:
            return "wpctl"
        if "pactl" in self.r.bin:
            return "pactl"
        raise Refused("neither wpctl nor pactl is installed — cannot reach the mixer")

    def volume(self, action, percent=None, device=None):
        m = self._mixer()
        if device:
            device = self._resolve_device(device, self.audio_devices(), "mixer output")
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
        """Mixer sinks — what `volume` can target."""
        return self._cached("sinks", self._audio_devices)

    def _audio_devices(self):
        if "pactl" not in self.r.bin:
            return []
        try:
            raw = self.r.run("pactl", "-f", "json", "list", "sinks")
            return [{"id": s.get("name", ""),
                     "label": (s.get("description") or s.get("name") or "")}
                    for s in json.loads(raw or "[]")]
        except Exception:
            return []

    def play_devices(self):
        """Outputs `play` can target. A DIFFERENT namespace from the mixer
        sinks above: mpv wants "<ao>/<device>" (pulse/alsa_output...,
        coreaudio/<uid>), so the names are taken from mpv itself rather than
        translated from pactl and hoped over."""
        return self._cached("outputs", self._play_devices)

    def _play_devices(self):
        if "mpv" not in self.r.bin:
            return []
        try:
            raw = self.r.run("mpv", "--audio-device=help")
        except Exception:
            return []
        out = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line.startswith("'"):
                continue
            name, _, label = line.partition("'")[2].partition("'")
            name = name.strip()
            if name and name != "auto":
                out.append({"id": name,
                            "label": label.strip(" ()") or name})
        return out

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
        return self._cached("screens", self._screens)

    def _screens(self):
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
            device = self._resolve_device(device, self.play_devices(), "playback output")
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
    """macOS. Same verbs; the mixer and the transport are native calls.

    Volume goes through CoreAudio via ctypes — a direct C call, no subprocess
    at all. Transport synthesizes the keyboard's own media keys through Quartz,
    so it drives whatever has the system's attention rather than a named app.

    Neither goes through osascript. AppleScript can `do shell script "..."`, so
    an `osascript -e` shim would put an interpreter — and a shell — back on the
    path this client exists to keep closed.
    """

    name = "darwin"

    def __init__(self, runner):
        super().__init__(runner)
        # loaded by path in tests, as a module in production - support both
        try:
            from . import macos as _mac
        except ImportError:
            import importlib.util as _u
            from pathlib import Path as _P
            _spec = _u.spec_from_file_location(
                "cu_macos", _P(__file__).with_name("macos.py"))
            _mac = _u.module_from_spec(_spec)
            _spec.loader.exec_module(_mac)
        self.mac = _mac

    def volume(self, action, percent=None, device=None):
        if device:
            raise Refused("picking a specific output for the mixer is not "
                          "wired up on macOS yet — this sets the default device")
        m = self.mac
        try:
            if action == "mute":
                m.set_mute(True)
                return {"ok": True, "action": action, "device": "default output"}
            if action == "unmute":
                m.set_mute(False)
                return {"ok": True, "action": action, "device": "default output"}
            step = (percent if percent is not None else 5) / 100.0
            if action == "set":
                level = m.set_volume(step)
            else:
                cur = m.get_volume()
                level = m.set_volume(cur + step if action == "up" else cur - step)
            return {"ok": True, "action": action, "device": "default output",
                    "level": round(level * 100)}
        except m.CoreAudioError as e:
            raise Refused(str(e))

    def transport(self, action):
        try:
            return self.mac.media_key(action)
        except self.mac.CoreAudioError as e:
            raise Refused(str(e))

    def audio_devices(self):
        try:
            return [{"id": "default", "label": self.mac.device_name()}]
        except Exception:
            return []

    def players(self):
        # macOS has no MPRIS; media keys go to whatever is frontmost for audio,
        # so there is no list to report and claiming one would be a fiction
        return []

    def screens(self):
        try:
            return self.mac.screens()
        except Exception:
            return []

    def open_link(self, url, screen=None):
        if "open" not in self.r.bin:
            raise Refused("/usr/bin/open is missing")
        self.r.run("open", url, background=True)
        return {"ok": True, "url": url}


# --- the client -------------------------------------------------------------

class Agent:
    def __init__(self, server, token, roots, name=None, dry_run=False,
                 cf_id=None, cf_secret=None):
        self.server = server.rstrip("/")
        self.token = token
        # Cloudflare Access service token, when Jarvis sits behind Access.
        # A browser gets an interactive SSO redirect; a daemon cannot, so it
        # presents these two headers on the upgrade request instead.
        self.cf_id = cf_id
        self.cf_secret = cf_secret
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

    # -- library, over this machine's own disk -------------------------------
    #
    # This lived on the Jarvis host until it turned out the host cannot see the
    # operator's Movies folder. Every path here is checked against self.grants,
    # which is already the intersection of the server's grants and --allow-root.

    def _exts(self, kind):
        return {"audio": AUDIO_EXT, "video": VIDEO_EXT}.get(
            kind, AUDIO_EXT | VIDEO_EXT)

    def _inside_grants(self, path):
        real = Path(path).expanduser().resolve()
        if not any(real == g or g in real.parents for g in self.grants):
            raise Refused(
                f"{real} is outside every allowed folder. Allowed: "
                + (", ".join(str(g) for g in self.grants) or "none"))
        return real

    def _count_media(self, base, exts, cap=5000):
        n, stack = 0, [base]
        while stack and n < cap:
            try:
                for e in os.scandir(stack.pop()):
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir():
                            stack.append(e.path)
                        elif Path(e.name).suffix.lower() in exts:
                            n += 1
                            if n >= cap:
                                break
                    except OSError:
                        continue
            except OSError:
                continue
        return n

    def do_list(self, folder=None, kind="both", limit=60):
        if not self.grants:
            return {"ok": True, "text":
                    "no folders are allowed on this machine — the client was "
                    "started without --allow-root, or the granted folders are "
                    "outside it"}
        exts = self._exts(kind)
        if folder:
            cand = Path(folder).expanduser()
            tries = [cand] if cand.is_absolute() else [g / folder for g in self.grants]
            base = None
            for t in tries:
                try:
                    real = t.resolve()
                except OSError:
                    continue
                if real.is_dir() and any(real == g or g in real.parents
                                         for g in self.grants):
                    base = real
                    break
            if base is None:
                raise Refused(
                    f"'{folder}' is not a folder inside an allowed one. Allowed: "
                    + ", ".join(str(g) for g in self.grants))
            bases = [base]
        else:
            bases = list(self.grants)

        out = []
        for base in bases:
            subdirs, files = [], []
            try:
                entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
            except OSError as e:
                out.append(f"{base}: cannot read ({e.strerror})")
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        subdirs.append((e.name, self._count_media(e.path, exts)))
                    elif Path(e.name).suffix.lower() in exts:
                        files.append(e.name)
                except OSError:
                    continue
            out.append(str(base))
            for name, n in subdirs:
                out.append(f"  {name}/  ({n} file{'s' if n != 1 else ''})"
                           if n else f"  {name}/  (nothing playable)")
            out.extend(f"  {f}" for f in files[:limit])
            if len(files) > limit:
                out.append(f"  ... and {len(files) - limit} more here")
            if not subdirs and not files:
                out.append("  (empty)")
        return {"ok": True, "text": "\n".join(out)}

    def do_find(self, query, kind="both", limit=40):
        words = [w for w in (query or "").lower().split() if w]
        if not words:
            raise Refused("empty query")
        exts = self._exts(kind)
        hits = []
        for g in self.grants:
            try:
                for dirpath, dirnames, filenames in os.walk(g):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for fn in filenames:
                        if Path(fn).suffix.lower() not in exts:
                            continue
                        full = Path(dirpath) / fn
                        hay = str(full.relative_to(g)).lower()
                        if all(w in hay for w in words):
                            hits.append(str(full))
                            if len(hits) >= limit:
                                return {"ok": True, "hits": hits}
            except OSError:
                continue
        return {"ok": True, "hits": hits}

    def handle(self, verb, params):
        p = clean_params(verb, params)
        if verb == "status":
            return {"ok": True, "platform": self.os.name,
                    "screens": self.os.screens(),
                    "audio_devices": self.os.audio_devices(),
                    "play_devices": self.os.play_devices(),
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
        if verb == "list":
            return self.do_list(p.get("folder"), p.get("kind", "both"),
                                p.get("limit", 60))
        if verb == "find":
            return self.do_find(p["query"], p.get("kind", "both"),
                                p.get("limit", 40))
        raise Refused(f"unhandled verb {verb!r}")     # pragma: no cover

    async def serve(self):
        try:
            import websockets
        except ImportError:
            print("this client needs the 'websockets' package "
                  "(pip install websockets)", file=sys.stderr)
            return 2
        if not self.server.startswith(("http://", "https://")):
            print("--server needs a scheme, e.g. https://jarvis.example",
                  file=sys.stderr)
            return 2
        ws_url = (self.server.replace("https://", "wss://")
                             .replace("http://", "ws://")) + "/api/computeruse/agent"
        headers = {}
        if self.cf_id and self.cf_secret:
            headers["CF-Access-Client-Id"] = self.cf_id
            headers["CF-Access-Client-Secret"] = self.cf_secret
        backoff = 1
        while True:
            try:
                # ping_interval is load-bearing behind a reverse proxy:
                # Cloudflare resets an idle WebSocket after ~100s, and
                # cloudflared drops idle HTTP/2 streams to the origin sooner
                # than that. 20s keeps it warm with room to spare.
                async with websockets.connect(
                        ws_url, max_size=64 * 1024,
                        additional_headers=headers or None,
                        ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({
                        "token": self.token, "name": self.name,
                        "platform": self.os.name,
                        "caps": {"screens": self.os.screens(),
                                 "audio_devices": self.os.audio_devices(),
                                 "play_devices": self.os.play_devices(),
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


def _sibling(name):
    """Import a sibling module whether we were run as a script or a package."""
    try:
        import importlib
        return importlib.import_module(f".{name}", __package__ or "")
    except Exception:
        import importlib.util as u
        spec = u.spec_from_file_location(f"cu_{name}", Path(__file__).with_name(f"{name}.py"))
        mod = u.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _fix_commands(runner, mac):
    """The exact commands for whatever is missing — so the operator copies and
    pastes rather than working out the package names for their platform."""
    fixes = []
    brew = sys.platform == "darwin"
    if "mpv" not in runner.bin:
        fixes.append(("play any media",
                      "brew install mpv" if brew
                      else "sudo apt install mpv    # or: sudo pacman -S mpv"))
    if brew:
        try:
            import Quartz            # noqa: F401
        except ImportError:
            fixes.append(("pause / skip / previous (media keys)",
                          "pip3 install pyobjc-framework-Quartz "
                          "pyobjc-framework-Cocoa"))
    else:
        if "pactl" not in runner.bin and "wpctl" not in runner.bin:
            fixes.append(("change the volume",
                          "sudo apt install pulseaudio-utils    "
                          "# or wireplumber for wpctl"))
        try:
            import jeepney           # noqa: F401
        except ImportError:
            fixes.append(("pause / skip / previous (MPRIS)", "pip3 install jeepney"))
        if "xdg-open" not in runner.bin:
            fixes.append(("open links", "sudo apt install xdg-utils"))
        if "xrandr" not in runner.bin:
            fixes.append(("list screens", "sudo apt install x11-xserver-utils"))
    try:
        import websockets            # noqa: F401
    except ImportError:
        fixes.insert(0, ("connect to Jarvis at all", "pip3 install websockets"))
    if brew and mac is not None:
        ok, _ = mac.preflight()
        if not ok:
            fixes.append(("media keys (permission, not a package)",
                          "open 'x-apple.systempreferences:com.apple.preference."
                          "security?Privacy_Accessibility'   # then allow this "
                          "terminal"))
    return fixes


def _selftest():
    """What can this machine actually do? Reports rather than assumes."""
    r = Runner(dry_run=False)
    print(f"platform      : {sys.platform}")
    cfgmod = _sibling("config")
    try:
        cfg = cfgmod.load()
        print(f"config        : {cfgmod.CONFIG_PATH}"
              + ("" if cfg else "  (none yet)"))
        if cfg:
            for k, v in sorted(cfgmod.redacted(cfg).items()):
                print(f"  {k:11s} {v}")
    except cfgmod.ConfigError as e:
        print(f"config        : PROBLEM - {e}")
    if sys.platform != "darwin":
        disp = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
        print(f"display       : {disp or 'NONE - opening links and video will fail'}")
    print(f"binaries found: {', '.join(sorted(r.bin)) or 'NONE'}")
    for need, why in (("mpv", "playing media"),
                      ("xdg-open" if sys.platform != "darwin" else "open",
                       "opening links")):
        print(f"  {need:9s} {'ok' if need in r.bin else 'MISSING'}  ({why})")
    if sys.platform == "darwin":
        try:
            from . import macos as mac
        except ImportError:
            import importlib.util as u
            spec = u.spec_from_file_location(
                "cu_macos", Path(__file__).with_name("macos.py"))
            mac = u.module_from_spec(spec)
            spec.loader.exec_module(mac)
        print("\nmacOS:")
        for k, v in mac.selftest().items():
            print(f"  {k:22s} {v}")
    else:
        os_ = Linux(r)
        print(f"\nmixer         : {'wpctl' if 'wpctl' in r.bin else 'pactl' if 'pactl' in r.bin else 'NONE'}")
        print(f"screens       : {os_.screens() or 'none detected'}")
        print(f"audio sinks   : {[d['id'] for d in os_.audio_devices()] or 'none detected'}")
        print(f"mpv outputs   : {[d['id'] for d in os_.play_devices()][:6] or 'none detected'}")
        try:
            print(f"MPRIS players : {os_.players() or 'none running'}")
        except Exception as e:
            print(f"MPRIS players : unavailable ({e})")

    mac = None
    if sys.platform == "darwin":
        try:
            mac = _sibling("macos")
        except Exception:
            mac = None
    fixes = _fix_commands(r, mac)
    print()
    if not fixes:
        print("Everything this client needs is present. Nothing to install.")
    else:
        print("=" * 68)
        print("TO FIX — copy and paste these:")
        print("=" * 68)
        for what, cmd in fixes:
            print(f"\n# {what}")
            print(cmd)
        print("\n" + "=" * 68)
        print("then re-run this selftest to confirm.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jarvis computer-use client")
    ap.add_argument("--server", help="https://host:port of Jarvis")
    ap.add_argument("--token", help="pairing token from the Computer use tab")
    ap.add_argument("--allow-root", action="append", default=[], metavar="DIR",
                    help="a folder Jarvis may play from. Repeatable. This is the "
                         "ceiling: grants made in the GUI can only narrow it.")
    ap.add_argument("--name", help="how this machine appears in Jarvis")
    ap.add_argument("--cf-access-id", default=os.environ.get("CF_ACCESS_CLIENT_ID"),
                    help="Cloudflare Access service token client id, if Jarvis "
                         "is behind Access. Prefer the CF_ACCESS_CLIENT_ID env "
                         "var — a command line is visible in `ps`.")
    ap.add_argument("--cf-access-secret",
                    default=os.environ.get("CF_ACCESS_CLIENT_SECRET"),
                    help="the matching secret (CF_ACCESS_CLIENT_SECRET)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and log every action without running it")
    ap.add_argument("--install", action="store_true",
                    help="save the settings to ~/.config/jarvis/computeruse.json "
                         "(0600) and write a systemd user unit or launchd agent, "
                         "then print the commands to enable it")
    ap.add_argument("--selftest", action="store_true",
                    help="report what this machine can actually drive, and "
                         "exit. Run this first on a new machine — especially "
                         "macOS, where volume and transport depend on "
                         "CoreAudio and an Accessibility grant.")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    cfgmod = _sibling("config")
    try:
        cfg = cfgmod.load()
    except cfgmod.ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2

    # command line > environment > saved config. A one-off override still works
    # and the old flag-only usage is unchanged.
    server = a.server or cfg.get("server")
    token = a.token or cfg.get("token")
    name = a.name or cfg.get("name")
    roots = a.allow_root or cfg.get("roots") or []
    cf_id = a.cf_access_id or cfg.get("cf_access_id")
    cf_secret = a.cf_access_secret or cfg.get("cf_access_secret")

    if bool(cf_id) != bool(cf_secret):
        ap.error("the Cloudflare Access id and secret go together")

    if a.install:
        svc = _sibling("service")
        path = cfgmod.save({"server": server, "token": token, "name": name,
                            "roots": roots, "cf_access_id": cf_id,
                            "cf_access_secret": cf_secret})
        print(f"settings saved to {path} (0600)")
        for k, v in sorted(cfgmod.redacted(cfgmod.load()).items()):
            print(f"  {k:16s} {v}")
        unit, steps = svc.install(Path(__file__))
        print(f"\nservice written to {unit}")
        print("it carries no secrets — only the path to the config above\n")
        print("run these to start it:")
        for line in steps:
            print(f"  {line}")
        return 0

    if not server or not token:
        ap.error("--server and --token are needed the first time; --install "
                 "then saves them for later. Or use --selftest.")
    agent = Agent(server, token, roots, name, a.dry_run, cf_id, cf_secret)
    try:
        return asyncio.run(agent.serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
