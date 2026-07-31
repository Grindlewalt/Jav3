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

WHICH FOLDERS THIS CAN REACH

The grant list on Jarvis's Computer use tab, and nothing else. It arrives when we
connect and again whenever the operator changes it, and it is applied live.

--allow-root now only seeds that list for the moments before Jarvis answers; it
used to be a hard ceiling that GUI grants could only narrow. That was dropped on
the operator's instruction because it made the tab dishonest — granting a folder
the client had not been launched with appeared to work and reached nothing, and
the only cure was re-running the set-up command. The containment checks are
unchanged and still enforced on this side: every path is resolved and must sit
inside a granted folder, and a granted folder must really be a directory here.
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

# Every HTTP request this client makes says who it is. Not politeness — the
# default is "Python-urllib/3.x", and Cloudflare's bot rules answer that with a
# 403. It cost a long evening: the set-up command's curl download succeeded, and
# the very next step, the same URL from urllib, was refused. Same host, same
# service token, same pairing token — the only difference on the wire was this
# header. A WebSocket connection was fine throughout, because the websockets
# library sends a user-agent of its own, which is what made it look like the
# HTTP routes specifically were broken.
USER_AGENT = "jarvis-computeruse/1.0"

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
        # platform.node() gives "MacBook-Pro-5.local"; the trailing .local and
        # the shouting are noise in something the model has to say back
        self.name = (name or (platform.node() or "desktop")
                     .removesuffix(".local").lower() or "desktop")
        self.dry_run = dry_run
        self.runner = Runner(dry_run=dry_run)
        self.os = MacOS(self.runner) if sys.platform == "darwin" else Linux(self.runner)
        # Folders named at startup. These are a STARTING POINT, not a ceiling —
        # see set_grants. They are what this client can reach until Jarvis says
        # otherwise, which it does on the very first message after connecting.
        self.roots = []
        for r in roots:
            p = Path(r).expanduser().resolve()
            if p.is_dir():
                self.roots.append(p)
            else:
                print(f"! --allow-root {r} is not a directory; ignoring", flush=True)
        self.grants = list(self.roots)
        self.grant_note = ""

    def set_grants(self, roots):
        """Adopt the folder list from Jarvis. It is authoritative.

        This used to intersect with --allow-root, which was the ceiling: the GUI
        could only ever narrow what the command line already permitted. That is
        a real control, and it was removed deliberately (operator's call), for
        one reason — it made the Computer use tab a liar. Granting a folder the
        client had not been started with looked like it worked, and then
        silently reached nothing; the only way to actually add a folder was to
        stop the client and re-run the set-up command with another --allow-root.
        Folders are the thing that changes most often, and that made them the
        one thing the GUI could not change.

        So the grant list in Jarvis is now simply what this client uses, and it
        is applied live (see _one) rather than only at connect. What remains of
        the old model: a folder still has to exist and be a directory here, the
        containment checks below are unchanged and still run on this side, and
        no verb reaches anything outside the list.
        """
        self.grant_note = ""
        sent = list(roots or [])
        kept, missing = [], []
        for r in sent:
            try:
                p = Path(r).expanduser().resolve()
            except OSError:
                missing.append(r)
                continue
            # A path that is not a directory HERE is the common mistake now that
            # the GUI can name anything: a Linux path granted to the Mac, or a
            # typo. Dropping it silently would look like the old ceiling bug.
            if p.is_dir():
                kept.append(p)
            else:
                missing.append(r)
        self.grants = kept
        if not sent:
            self.grant_note = ("no folders are granted in Jarvis, so nothing on "
                               "disk is reachable — add one on the Computer use tab")
        elif missing:
            self.grant_note = (
                "granted but not a folder on this machine: " + ", ".join(missing)
                + " — check the path is right for this computer")
        if self.grant_note:
            print(f"! {self.grant_note}", flush=True)

    def save_access_token(self, cfg):
        """Write a pushed Access token into our own 0600 config.

        Deliberately narrow: only the two Access fields are read out of whatever
        arrives. A general "apply this config" message would let the server
        rewrite our server address or our folder roots, and there is no reason
        to accept that — this exists for one thing.

        Never printed. The whole point of the config file being 0600 is that the
        secret is not lying around in scrollback, log files, or a screenshot of
        this terminal.

        Returns (ok, error) so the caller can answer Jarvis truthfully.
        """
        cid = str(cfg.get("cf_access_id") or "").strip()
        sec = str(cfg.get("cf_access_secret") or "").strip()
        if not (cid and sec):
            return False, "the pushed token was incomplete"
        if (cid, sec) == (self.cf_id, self.cf_secret):
            return True, ""             # already current; say nothing
        try:
            cfgmod = _sibling("config")
            saved = cfgmod.load()
            saved.update({"cf_access_id": cid, "cf_access_secret": sec})
            cfgmod.save(saved)
        except Exception as e:
            # the class name only: the exception text can quote the file, and
            # for a JSON error that means quoting what is in it
            print(f"! could not save the new Access token: "
                  f"{e.__class__.__name__}", file=sys.stderr, flush=True)
            return False, f"could not save it: {e.__class__.__name__}"
        self.cf_id, self.cf_secret = cid, sec
        print("Cloudflare Access token updated from Jarvis; it takes effect on "
              "the next reconnect", flush=True)
        return True, ""

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
            roots = []
            for g in self.grants:
                roots.append({
                    "path": str(g),
                    "audio": self._count_media(g, AUDIO_EXT),
                    "video": self._count_media(g, VIDEO_EXT)})
            return {"ok": True, "platform": self.os.name,
                    "roots_detail": roots,
                    "grant_note": getattr(self, "grant_note", ""),
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
        # The same name the HTTP calls give. This handshake was never the one
        # being blocked — the websockets library sends a user-agent of its own,
        # which is exactly why the socket worked while the plain HTTP routes
        # 403'd, and why the fault looked like it was in those routes.
        headers = {"User-Agent": USER_AGENT}
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
        # A folder list, pushed because the operator changed it in the GUI. It
        # is not a verb and wants no reply — without this branch it fell through
        # as verb=None and answered an error to a request nobody made. This is
        # what makes a new grant work now instead of on the next restart.
        if verb is None and "grants" in msg:
            self.set_grants(msg.get("grants") or [])
            print(f"folders updated: {[str(g) for g in self.grants]}", flush=True)
            return
        # A rotated Cloudflare Access service token, pushed down the socket we
        # are already authenticated on. Saved for the NEXT connection — this one
        # keeps working on the credentials it was accepted with, so a rotation
        # costs no interruption. Without this, rotating meant visiting every
        # machine, because a client with a stale token cannot reach Jarvis to
        # be told anything.
        if verb is None and isinstance(msg.get("config"), dict):
            ok, err = self.save_access_token(msg["config"])
            # Answered, so Jarvis can tell the operator which machines really
            # took the rotation. A build that predates this branch replies with
            # an error instead, which is the correct answer for it.
            if req:
                await ws.send(json.dumps(
                    {"id": req, "ok": ok, **({} if ok else {"error": err})}))
            return
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


# --- what is missing here, and the exact command to fix it -------------------
#
# The install line is different on every distribution, and the first version of
# this printed `sudo apt install ...` on all of them. On Arch that is a
# "command not found" with the package names guessed as well, which leaves the
# operator translating the advice before they can take it. So: find the manager
# this machine actually has, then look the names up for it.

INSTALLERS = (
    ("pacman", "sudo pacman -S --needed"),
    ("apt-get", "sudo apt install"),
    ("dnf", "sudo dnf install"),
    ("zypper", "sudo zypper install"),
    ("apk", "sudo apk add"),
    ("brew", "brew install"),
)

# need -> package name per manager; "*" is what it is called everywhere else.
# pactl is in libpulse on Arch and pulseaudio-utils on Debian; xrandr is in
# xorg-xrandr on Arch and x11-xserver-utils on Debian. Same binary, three names.
PACKAGES = {
    "mpv":      {"*": "mpv"},
    "mixer":    {"*": "pulseaudio-utils", "pacman": "libpulse"},
    "xdg-open": {"*": "xdg-utils"},
    "xrandr":   {"*": "xrandr", "pacman": "xorg-xrandr",
                 "apt-get": "x11-xserver-utils"},
}


def _installer():
    """(manager, install command) for this machine, or (None, None)."""
    for name, cmd in INSTALLERS:
        if shutil.which(name):
            return name, cmd
    return None, None


def _package(need, manager):
    names = PACKAGES.get(need, {})
    return names.get(manager) or names.get("*") or need


def _missing(runner, mac=None):
    """Everything absent, as {why, and one of pkg / pip / note}."""
    out = []
    brew = sys.platform == "darwin"
    try:
        import websockets            # noqa: F401
    except ImportError:
        out.append({"why": "connect to Jarvis at all", "pip": ["websockets"]})
    if "mpv" not in runner.bin:
        out.append({"why": "play any media", "pkg": "mpv"})
    if brew:
        try:
            import Quartz            # noqa: F401
        except ImportError:
            out.append({"why": "pause / skip / previous (media keys)",
                        "pip": ["pyobjc-framework-Quartz",
                                "pyobjc-framework-Cocoa"]})
        if mac is not None:
            ok, _ = mac.preflight()
            if not ok:
                out.append({
                    "why": "media keys — a permission, not a package",
                    "note": "System Settings > Privacy & Security > "
                            "Accessibility, and add the terminal (or the python "
                            "binary) running this"})
    else:
        if "pactl" not in runner.bin and "wpctl" not in runner.bin:
            out.append({"why": "change the volume", "pkg": "mixer"})
        try:
            import jeepney           # noqa: F401
        except ImportError:
            out.append({"why": "pause / skip / previous (MPRIS)",
                        "pip": ["jeepney"]})
        if "xdg-open" not in runner.bin:
            out.append({"why": "open links", "pkg": "xdg-open"})
        if "xrandr" not in runner.bin:
            out.append({"why": "list screens", "pkg": "xrandr"})
    return out


def _print_fixes(missing):
    """One command per packaging system rather than one per package — four
    consecutive `sudo apt install` lines is four password prompts to fix what is
    one install."""
    if not missing:
        print("Everything this client needs is present. Nothing to install.")
        return
    manager, install = _installer()
    print("=" * 68)
    print("MISSING — the client still runs; this is what it cannot do:")
    print("=" * 68)
    for m in missing:
        label = m.get("pkg") or (m.get("pip") or ["permission"])[0]
        print(f"  {label:<16} {m['why']}")
    pkgs = [_package(m["pkg"], manager) for m in missing if m.get("pkg")]
    pips = [p for m in missing for p in m.get("pip", [])]
    print()
    if pkgs and install:
        print(f"  {install} {' '.join(pkgs)}")
    elif pkgs:
        print("  no package manager I recognise here. Install: " + ", ".join(pkgs))
    if pips:
        # sys.executable, not pip3. The deps belong in the venv this client will
        # be run from; a system pip is refused outright on Arch and Debian
        # (PEP 668) and a pip3 from somewhere else installs them where nothing
        # will look.
        print(f"  {sys.executable} -m pip install {' '.join(pips)}")
    for m in missing:
        if m.get("note"):
            print(f"\n  {m['why']}:\n    {m['note']}")
    print()
    print("then re-run with --selftest to confirm.")


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
    print()
    _print_fixes(_missing(r, mac))
    return 0


def _ping(server, token, cf_id=None, cf_secret=None):
    """One plain HTTP request to Jarvis before anything else. Returns (ok, why).

    Every way this can be wrong — a typo in the URL, no Cloudflare service
    token, a pairing token that has since been rotated — looks identical from
    inside the WebSocket retry loop: "disconnected (InvalidStatus: server
    rejected the connection); retrying in 1s", forever, with the reason on the
    far side of a handshake that never completes. Asking over ordinary HTTP
    first is the difference between a sentence and a loop.
    """
    import urllib.error
    import urllib.request

    if not server.startswith(("http://", "https://")):
        return False, f"--server {server!r} needs a scheme, e.g. https://host"
    url = server.rstrip("/") + "/api/computeruse/ping"
    req = urllib.request.Request(url, headers={"X-Jarvis-Token": token,
                                               "User-Agent": USER_AGENT})
    if cf_id and cf_secret:
        req.add_header("CF-Access-Client-Id", cf_id)
        req.add_header("CF-Access-Client-Secret", cf_secret)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(8192)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, ("Jarvis answered, and refused the pairing token. It "
                           "is rotated from the Computer use tab — copy the "
                           "current one.")
        if e.code == 403:
            return False, (
                "403 from whatever fronts Jarvis — and note that Cloudflare "
                "Access does NOT answer 403; an unauthenticated request to it "
                "comes back as a 302 to a login page. So this is a firewall or "
                "bot rule, not a missing service token. The usual cause is the "
                "user-agent: this client names itself, but a plain 'curl' or a "
                "browser-less request can trip Bot Fight Mode. Check the WAF / "
                "Bot Fight Mode rules for the hostname rather than the Access "
                "policy.")
        if e.code == 404:
            # an older Jarvis, from before this route existed. The socket is
            # what matters and that has not moved, so this is not a stop.
            return True, f"reached {server} (no ping route — an older Jarvis)"
        return False, f"{server} answered {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return False, (f"cannot reach {server}: {e.reason}. Check the address, "
                       f"and that this machine can see it.")
    except OSError as e:
        return False, f"cannot reach {server}: {e}"
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        # An HTML login page, almost always: Access redirecting a request with
        # no service token to SSO, which a daemon can never complete.
        return False, (f"{server} returned a web page rather than Jarvis. That "
                       f"is usually Cloudflare Access asking for a browser "
                       f"login — add --cf-access-id and --cf-access-secret.")
    if not isinstance(body, dict) or not body.get("ok"):
        return False, f"{server} answered, but not as Jarvis: {raw[:120]!r}"
    others = [c for c in (body.get("connected") or [])]
    note = f"; already connected: {', '.join(others)}" if others else ""
    return True, f"reached {server}, token accepted{note}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Jarvis computer-use client")
    ap.add_argument("--server", help="https://host:port of Jarvis")
    ap.add_argument("--token", help="pairing token from the Computer use tab")
    ap.add_argument("--allow-root", action="append", default=[], metavar="DIR",
                    help="a folder Jarvis may play from, before it sends its own "
                         "list. Repeatable, and optional — the grants on the "
                         "Computer use tab replace this on connect and whenever "
                         "you change them, so folders are managed there now.")
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
    ap.add_argument("--setup", action="store_true",
                    help="first run, all of it: check Jarvis is reachable and "
                         "the token works, save the settings, report what this "
                         "machine can drive and how to install the rest, then "
                         "connect in the foreground. --install afterwards to "
                         "keep it running.")
    ap.add_argument("--install", action="store_true",
                    help="save the settings to ~/.config/jarvis/computeruse.json "
                         "(0600) and write a systemd user unit or launchd agent, "
                         "then print the commands to enable it")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the service definition this client installed, "
                         "and print how to stop it. Add --purge to delete the "
                         "saved settings and pairing token too.")
    ap.add_argument("--purge", action="store_true",
                    help="with --uninstall, also delete "
                         "~/.config/jarvis/computeruse.json — use this when "
                         "starting over rather than reinstalling")
    ap.add_argument("--selftest", action="store_true",
                    help="report what this machine can actually drive, and "
                         "exit. Run this first on a new machine — especially "
                         "macOS, where volume and transport depend on "
                         "CoreAudio and an Accessibility grant.")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    cfgmod = _sibling("config")
    if a.uninstall:
        # Before the config is loaded: a config this refuses to read (bad mode,
        # bad JSON) is exactly when someone wants to remove it, and bailing out
        # there would leave them with no way to undo an install.
        svc = _sibling("service")
        removed, steps = svc.uninstall()
        if a.purge and cfgmod.CONFIG_PATH.exists():
            cfgmod.CONFIG_PATH.unlink()
            removed.append(cfgmod.CONFIG_PATH)
        for p in removed:
            print(f"removed {p}")
        if not removed:
            print("nothing to remove — no service definition was installed here")
        if not a.purge:
            print(f"kept    {cfgmod.CONFIG_PATH} (--purge deletes it, token and all)")
        print("\nStill running until you run:")
        for line in steps:
            print(f"  {line}")
        print("\nThe client source itself is just this folder — delete it when "
              "you are done.")
        return 0

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

    if a.install and not (server and token):
        # --install is now run bare, after --setup has saved everything. Without
        # that it would write a unit for a config with no server in it, which
        # starts and dies on every restart forever.
        ap.error(f"nothing to save: no --server/--token given, and none in "
                 f"{cfgmod.CONFIG_PATH}. Run --setup first.")

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
        print("it carries no secrets — only the path to the config above")
        print()
        print("=" * 68)
        print("NOTHING IS RUNNING YET. This wrote files; it did not start")
        print("anything, so Jarvis will not show this machine as connected")
        print("until you run:")
        print("=" * 68)
        for line in steps:
            print(f"  {line}")
        print()
        print("Or, to watch it connect in the foreground first:")
        print(f"  {sys.executable} {Path(__file__).resolve()}")
        return 0

    if not server or not token:
        ap.error("--server and --token are needed the first time. The Computer "
                 "use tab builds the whole command — it ends in --setup, which "
                 "saves them for every run after this one. Or --selftest.")

    if a.setup:
        # Order matters. Reaching Jarvis is checked FIRST, because everything
        # below it is wasted if the address or the token is wrong, and because
        # it is the one failure the operator cannot diagnose from here.
        print("1/4  reaching Jarvis")
        ok, why = _ping(server, token, cf_id, cf_secret)
        print(f"     {why}")
        if not ok:
            return 2

        print("\n2/4  saving settings")
        path = cfgmod.save({"server": server, "token": token, "name": name,
                            "roots": roots, "cf_access_id": cf_id,
                            "cf_access_secret": cf_secret})
        print(f"     {path} (0600) — every later run reads this, so no flags")
        if not roots:
            print("     ! no --allow-root given, which is fine — folders are "
                  "managed in Jarvis.\n"
                  "       Add one on the Computer use tab and it reaches this "
                  "client straight\n"
                  "       away, no restart. Until then volume, links and "
                  "transport work but\n"
                  "       no file on this machine can be played.")

        print("\n3/4  what this machine can drive")
        _selftest()

        print("\n4/4  connecting — leave this running. It appears on the "
              "Computer use tab\n"
              "     the moment it connects; Ctrl-C when you are ready to make "
              "it permanent.")

    agent = Agent(server, token, roots, name, a.dry_run, cf_id, cf_secret)
    try:
        return asyncio.run(agent.serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
