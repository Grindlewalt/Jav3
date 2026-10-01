#!/usr/bin/env python3
"""Drive the real jav3 terminal client in a pty and read its screen, in ONE command.

The client (clients/jav3cli/jav3) runs unmodified under pexpect with TERM=xterm-256color
and no COLORTERM (what macOS Terminal.app gives it); pyte renders the bytes it writes.
Steps are joined with "|" (a literal pipe is "\\|"):

  scripts/tui_drive.py --fake "type:/vms|key:enter|wait:1|show"
  scripts/tui_drive.py --fake --size 80x24 "type:hello|key:enter|waitfor:Done|show|bg:"
  scripts/tui_drive.py --server pi "key:left|wait:2|show"        # the Pi, your saved login
  scripts/tui_drive.py "type:/security|key:enter|waitfor:Queue@15|show"   # saved login

Steps:
  type:TEXT           type TEXT (no Enter)        paste:TEXT   bracketed paste
  key:NAME[*N],...    enter escape tab shift+tab up down left right home end pageup
                      pagedown backspace delete space ctrl+x alt+x f1..f12 (comma = several)
  raw:ESCAPES         send the bytes (\\x1b[Z, \\r, ...)
  wait:SECS           keep reading for SECS
  waitfor:TEXT[@S]    wait until TEXT is on screen (S seconds, default --timeout); a timeout
                      prints the screen and exits 1.   waitgone:TEXT[@S] is the reverse
  expect:TEXT         fail (exit 1) unless TEXT is on screen now;  absent:TEXT the reverse
  show[:LABEL]        print the screen as text (trailing blank rows dropped)
  bg[:LABEL]          every row with its dominant fg/bg (xterm-256 index) and a LOW flag on
                      runs whose contrast against their background is under 3:1
  fg[:TEXT]           with TEXT: the fg/bg of each place TEXT is drawn; without: every
                      distinct fg/bg pair on screen, worst contrast first
  snap:FILE           write the screen (text + colour runs) to FILE
  resize:WxH          change the terminal size (SIGWINCH)

--fake serves tests/tui_fake_http.py (seeded chats, a running turn, boxes, security rows,
agents) with a throw-away config dir and the client's time.time() pinned to the fake's
clock. Without --fake the client uses your own ~/.config/jav3/credentials.json (never
read or printed here); --server pi is 10.0.0.82:8000, any other value goes to the
client's own --server. Exit status: 0, or 1 when a waitfor/expect failed or the client
died; 2 for a bad step.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CLI = REPO / "clients" / "jav3cli" / "jav3"
PI = "10.0.0.82:8000"
TERM = "xterm-256color"

KEYS = {
    "enter": "\r", "return": "\r", "escape": "\x1b", "esc": "\x1b", "tab": "\t",
    "shift+tab": "\x1b[Z", "backtab": "\x1b[Z", "space": " ", "backspace": "\x7f",
    "delete": "\x1b[3~", "insert": "\x1b[2~",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "pgup": "\x1b[5~", "pgdn": "\x1b[6~",
    "ctrl+up": "\x1b[1;5A", "ctrl+down": "\x1b[1;5B", "ctrl+right": "\x1b[1;5C",
    "ctrl+left": "\x1b[1;5D", "ctrl+home": "\x1b[1;5H", "ctrl+end": "\x1b[1;5F",
    "alt+up": "\x1b[1;3A", "alt+down": "\x1b[1;3B", "alt+right": "\x1b[1;3C",
    "alt+left": "\x1b[1;3D",
    # shift+arrows exist here for completeness; Terminal.app does not send them
    "shift+up": "\x1b[1;2A", "shift+down": "\x1b[1;2B", "shift+right": "\x1b[1;2C",
    "shift+left": "\x1b[1;2D",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS", "f5": "\x1b[15~",
    "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~", "f9": "\x1b[20~",
    "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}


def key_bytes(name: str) -> str:
    n = name.strip().lower()
    if n in KEYS:
        return KEYS[n]
    if n.startswith("ctrl+") and len(n) == 6 and n[5].isalpha():
        return chr(ord(n[5]) - 96)
    if n.startswith("alt+") and len(n) == 5:
        return "\x1b" + name.strip()[4]
    if len(name) == 1:
        return name
    raise StepError(f"unknown key {name!r}")


class StepError(Exception):
    """A step the driver cannot run (exit 2)."""


# --- the screen: pyte, plus the colour index the client chose ------------------------------

def _pyte():
    try:
        import pyte
    except ImportError:
        sys.exit("tui_drive needs pexpect and pyte: pip install pexpect pyte")
    return pyte


def make_screen(cols: int, rows: int):
    """A pyte screen that keeps the palette index of 256-colour SGR (pyte turns
    38;5;N into the colour's hex, which loses N and cannot tell it from truecolor).
    Cell colours come out as None (default), 0..255, or '#rrggbb' (truecolor)."""
    pyte = _pyte()
    names = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
    by_name = {n: i for i, n in enumerate(names)}
    by_name.update({"bright" + n: 8 + i for i, n in enumerate(names)})

    class Screen(pyte.Screen):
        def select_graphic_rendition(self, *attrs):
            i, chunk = 0, []
            while i < len(attrs):
                a = attrs[i]
                mode = attrs[i + 1] if i + 1 < len(attrs) else None
                if a in (38, 48) and mode in (5, 2):
                    need = 1 if mode == 5 else 3
                    args = attrs[i + 2:i + 2 + need]
                    if chunk:
                        super().select_graphic_rendition(*chunk)
                        chunk = []
                    if len(args) == need:
                        val = f"i{args[0]}" if mode == 5 else "#%02x%02x%02x" % tuple(args)
                        self.cursor.attrs = self.cursor.attrs._replace(
                            **{"fg" if a == 38 else "bg": val})
                    i += 2 + need
                    continue
                chunk.append(a)
                i += 1
            if chunk or not attrs:
                super().select_graphic_rendition(*chunk)

    def norm(c: str):
        if c == "default":
            return None
        if c.startswith("i") and c[1:].isdigit():
            return int(c[1:])
        if c.startswith("#"):
            return c
        if c in by_name:
            return by_name[c]
        return "#" + c if re.fullmatch(r"[0-9a-f]{6}", c) else c

    screen = Screen(cols, rows)
    screen.norm_colour = norm
    return screen


def cell_style(screen, ch) -> tuple:
    """(fg, bg, attrs) of a pyte Char; attrs is a string like 'br'."""
    attrs = "".join(k for k, on in (("b", ch.bold), ("i", ch.italics), ("u", ch.underscore),
                                    ("r", ch.reverse), ("s", ch.strikethrough),
                                    ("k", ch.blink)) if on)
    return screen.norm_colour(ch.fg), screen.norm_colour(ch.bg), attrs


def colour_name(c) -> str:
    return "-" if c is None else str(c)


def style_str(st: tuple) -> str:
    return f"{colour_name(st[0])}/{colour_name(st[1])}" + (f"+{st[2]}" if st[2] else "")


def screen_text(screen) -> list[str]:
    return [ln.rstrip() for ln in screen.display]


def snapshot_text(screen, title: str = "") -> str:
    """The whole screen as a diff-friendly text file: the rows, then one line per row
    listing runs of cells with the same colours: `07 0-9=75/234+b 10-79=252/234`
    (cols x0-x1, fg/bg as xterm-256 indexes, - = the terminal default, + attrs
    b bold i italic u underline r reverse s strike k blink; `*` = the whole row)."""
    rows, cols = screen.lines, screen.columns
    out = [f"# {title}".rstrip() + f"  {cols}x{rows} TERM={TERM}", "## text"]
    out += screen_text(screen)
    out.append("## colours (fg/bg as xterm-256 index, - = default)")
    for y in range(rows):
        row = screen.buffer[y]
        runs, start, cur = [], 0, None
        for x in range(cols):
            st = cell_style(screen, row[x])
            if st != cur:
                if cur is not None:
                    runs.append((start, x - 1, cur))
                start, cur = x, st
        runs.append((start, cols - 1, cur))
        if len(runs) == 1:
            body = f"*={style_str(runs[0][2])}"
        else:
            body = " ".join(f"{a}-{b}={style_str(st)}" for a, b, st in runs)
        out.append(f"{y:02d} {body}")
    return "\n".join(out) + "\n"


# --- contrast, the way a 256-colour Terminal.app shows it -----------------------------------

def xterm_rgb(i: int) -> tuple[int, int, int]:
    if i < 16:    # xterm's defaults; Terminal.app's own 0-15 differ a little
        base = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205),
                (0, 205, 205), (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0),
                (255, 255, 0), (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return base[i]
    if i < 232:
        n, steps = i - 16, (0, 95, 135, 175, 215, 255)
        return steps[n // 36], steps[(n // 6) % 6], steps[n % 6]
    v = 8 + (i - 232) * 10
    return v, v, v


def colour_rgb(c, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if c is None:
        return default
    if isinstance(c, int):
        return xterm_rgb(c)
    if isinstance(c, str) and re.fullmatch(r"#[0-9a-f]{6}", c):
        return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    return default


def luminance(rgb) -> float:
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


DEFAULT_FG, DEFAULT_BG = (229, 229, 229), (0, 0, 0)    # the profile's own; a guess
LOW = 3.0


def effective(st: tuple) -> tuple:
    """(fg, bg) after reverse video."""
    fg, bg, attrs = st
    return (bg, fg) if "r" in attrs else (fg, bg)


def style_contrast(st: tuple) -> float:
    fg, bg = effective(st)
    return contrast(colour_rgb(fg, DEFAULT_FG), colour_rgb(bg, DEFAULT_BG))


def text_runs(screen):
    """Yield (y, x0, x1, text, style) for each run of same-styled cells that holds ink."""
    for y in range(screen.lines):
        row = screen.buffer[y]
        x = 0
        while x < screen.columns:
            st = cell_style(screen, row[x])
            x1 = x
            while x1 + 1 < screen.columns and cell_style(screen, row[x1 + 1]) == st:
                x1 += 1
            txt = "".join(row[k].data for k in range(x, x1 + 1))
            if txt.strip():
                yield y, x, x1, txt, st
            x = x1 + 1


def report_bg(screen, label: str = "") -> str:
    out = [f"--- colours{' ' + label if label else ''} (fg/bg = xterm-256 index, - = default; "
           f"LOW = contrast < {LOW:g}:1) ---"]
    by_row: dict[int, list] = {}
    for y, x0, x1, txt, st in text_runs(screen):
        by_row.setdefault(y, []).append((x0, x1, txt, st))
    for y in range(screen.lines):
        runs = by_row.get(y)
        if not runs:
            continue
        weight_fg: dict = {}
        weight_bg: dict = {}
        for x0, x1, txt, st in runs:
            n = len(txt.strip())
            weight_fg[st[0]] = weight_fg.get(st[0], 0) + n
            weight_bg[st[1]] = weight_bg.get(st[1], 0) + n
        fg = max(weight_fg, key=weight_fg.get)
        bg = max(weight_bg, key=weight_bg.get)
        text = screen_text(screen)[y]
        out.append(f"{y:02d} fg {colour_name(fg):>4} bg {colour_name(bg):>4} | {text[:110]}")
        for x0, x1, txt, st in runs:
            c = style_contrast(st)
            if c < LOW:
                out.append(f"     LOW {c:.1f} {style_str(st)} cols {x0}-{x1} {txt.strip()[:50]!r}")
    return "\n".join(out)


def report_fg(screen, text: str = "") -> str:
    if text:
        out, seen = [f"--- colours of {text!r} ---"], 0
        for y in range(screen.lines):
            line = "".join(screen.buffer[y][x].data for x in range(screen.columns))
            for m in re.finditer(re.escape(text), line):
                st = cell_style(screen, screen.buffer[y][m.start()])
                seen += 1
                out.append(f"row {y} col {m.start()}: {style_str(st)} contrast "
                           f"{style_contrast(st):.1f}")
        if not seen:
            out.append("(not on screen)")
        return "\n".join(out)
    pairs: dict[tuple, list] = {}
    for y, x0, x1, txt, st in text_runs(screen):
        e = pairs.setdefault(st, [0, txt.strip()[:40]])
        e[0] += len(txt.strip())
    out = [f"--- distinct fg/bg pairs, worst contrast first (LOW < {LOW:g}:1) ---"]
    for st, (n, sample) in sorted(pairs.items(), key=lambda kv: style_contrast(kv[0])):
        c = style_contrast(st)
        out.append(f"{'LOW ' if c < LOW else '    '}{c:5.1f} {style_str(st):<14} {n:>4} chars  {sample!r}")
    return "\n".join(out)


# --- the session -------------------------------------------------------------------------

class Session:
    def __init__(self, argv: list[str], env: dict, cols: int, rows: int, cwd: str,
                 redact: list[str] | None = None) -> None:
        try:
            import pexpect
        except ImportError:
            sys.exit("tui_drive needs pexpect and pyte: pip install pexpect pyte")
        self.pexpect = pexpect
        self.cols, self.rows = cols, rows
        self.screen = make_screen(cols, rows)
        self.stream = _pyte().ByteStream(self.screen)
        self.redact = [s for s in (redact or []) if s]
        self.t0 = time.time()
        self.alive = True
        self.status: int | None = None
        self.child = pexpect.spawn(argv[0], argv[1:], env=env, dimensions=(rows, cols),
                                   cwd=cwd, encoding=None)

    def pump(self, secs: float) -> None:
        end = time.time() + secs
        while self.alive:
            left = end - time.time()
            if left <= 0:
                return
            self._read(min(left, 0.05))

    def _read(self, timeout: float) -> bool:
        try:
            data = self.child.read_nonblocking(65536, timeout=timeout)
        except self.pexpect.TIMEOUT:
            return False
        except self.pexpect.EOF:
            self.alive = False
            self.child.close()
            self.status = self.child.exitstatus if self.child.exitstatus is not None \
                else (128 + (self.child.signalstatus or 0))
            return False
        self.stream.feed(data)
        return True

    def settle(self, quiet: float = 0.25, cap: float = 3.0, first: bool = False) -> None:
        """Read until the client has been quiet for `quiet` seconds (at most `cap`):
        a snapshot is then a whole frame, not half of one. `first` waits for some output."""
        end = time.time() + cap
        last = time.time()
        got = False
        while self.alive and time.time() < end:
            if self._read(0.03):
                last, got = time.time(), True
            elif (got or not first) and time.time() - last >= quiet:
                return

    def send(self, data: str, quiet: float = 0.2) -> None:
        if not self.alive:
            return
        self.child.send(data.encode())
        self.settle(quiet=quiet, cap=1.5)

    def text(self) -> str:
        return "\n".join(screen_text(self.screen))

    def has(self, needle: str) -> bool:
        return needle in self.text()

    def wait_for(self, needle: str, secs: float, gone: bool = False) -> bool:
        end = time.time() + secs
        while True:
            if self.has(needle) != gone:
                self.settle(quiet=0.1, cap=0.5)
                return True
            if not self.alive or time.time() >= end:
                return False
            self._read(0.05)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.child.setwinsize(rows, cols)
        self.screen.resize(rows, cols)
        self.settle(quiet=0.3, cap=2.0)

    def close(self) -> None:
        if self.alive:
            self.child.terminate(force=True)
            self.alive = False
        elif self.status is None:
            self.status = 0


def split_steps(spec: str) -> list[str]:
    out, cur, i = [], [], 0
    while i < len(spec):
        c = spec[i]
        if c == "\\" and spec[i + 1:i + 2] == "|":
            cur.append("|")
            i += 2
            continue
        if c == "|":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [s for s in (x.strip("\n") for x in out) if s.strip()]


def wait_args(val: str, default: float) -> tuple[str, float]:
    head, sep, tail = val.rpartition("@")
    if sep:
        try:
            return head, float(tail)
        except ValueError:
            pass
    return val, default


class Runner:
    def __init__(self, sess: Session, timeout: float, out=sys.stdout) -> None:
        self.s, self.timeout, self.out = sess, timeout, out
        self.failed = False

    def emit(self, text: str) -> None:
        for secret in self.s.redact:
            text = text.replace(secret, "<redacted>")
        print(text, file=self.out, flush=True)

    def screen_dump(self, label: str) -> str:
        rows = screen_text(self.s.screen)
        while rows and not rows[-1]:
            rows.pop()
        return f"--- {label} ({self.s.cols}x{self.s.rows}, {time.time() - self.s.t0:.1f}s) ---\n" \
               + "\n".join(rows)

    def run(self, steps: list[str]) -> None:
        for st in steps:
            verb, _, val = st.partition(":")
            verb = verb.strip().lower()
            fn = getattr(self, "do_" + verb, None)
            if fn is None:
                raise StepError(f"unknown step {verb!r} (in {st[:40]!r})")
            fn(val)
            if self.failed:
                return

    # input
    def do_type(self, val: str) -> None:
        self.s.send(val, quiet=0.2)

    def do_paste(self, val: str) -> None:
        self.s.send("\x1b[200~" + val + "\x1b[201~", quiet=0.25)

    def do_key(self, val: str) -> None:
        for part in val.split(","):
            name, star, n = part.strip().partition("*")
            data = key_bytes(name) if name.strip() else ""
            for _ in range(int(n) if star else 1):
                self.s.send(data, quiet=0.15)

    def do_raw(self, val: str) -> None:
        self.s.send(val.encode().decode("unicode_escape"), quiet=0.2)

    def do_resize(self, val: str) -> None:
        m = re.fullmatch(r"\s*(\d+)x(\d+)\s*", val)
        if not m:
            raise StepError(f"resize wants WxH, got {val!r}")
        self.s.resize(int(m[1]), int(m[2]))

    # waiting and asserting
    def do_wait(self, val: str) -> None:
        self.s.pump(float(val or 1))
        self.s.settle(quiet=0.15, cap=1.0)

    do_sleep = do_wait

    def _wait(self, val: str, gone: bool) -> None:
        needle, secs = wait_args(val, self.timeout)
        if not self.s.wait_for(needle, secs, gone):
            why = "client exited" if not self.s.alive else f"timeout after {secs:g}s"
            self.emit(f"-- {'waitgone' if gone else 'waitfor'} {needle!r}: {why}")
            self.emit(self.screen_dump("screen at failure"))
            self.failed = True

    def do_waitfor(self, val: str) -> None:
        self._wait(val, False)

    def do_waitgone(self, val: str) -> None:
        self._wait(val, True)

    def do_expect(self, val: str) -> None:
        if not self.s.has(val):
            self.emit(f"-- expect {val!r}: not on screen")
            self.emit(self.screen_dump("screen at failure"))
            self.failed = True

    def do_absent(self, val: str) -> None:
        if self.s.has(val):
            self.emit(f"-- absent {val!r}: it is on screen")
            self.emit(self.screen_dump("screen at failure"))
            self.failed = True

    # output
    def do_show(self, val: str) -> None:
        self.emit(self.screen_dump(val.strip() or "screen"))

    def do_bg(self, val: str) -> None:
        self.emit(report_bg(self.s.screen, val.strip()))

    def do_fg(self, val: str) -> None:
        self.emit(report_fg(self.s.screen, val))

    def do_snap(self, val: str) -> None:
        p = Path(val.strip()).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(snapshot_text(self.s.screen, p.stem))
        self.emit(f"-- snapshot written: {p}")


# --- launching the client ----------------------------------------------------------------------

def parse_size(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"--size wants COLSxROWS, e.g. 120x40, got {s!r}")
    return int(m[1]), int(m[2])


def parse_clock(s: str, default: float) -> float | None:
    if s == "none":
        return None
    if s == "fake":
        return default
    try:
        return float(s)
    except ValueError:
        import calendar
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return float(calendar.timegm(time.strptime(s, fmt)))
            except ValueError:
                pass
    raise SystemExit(f"--clock: {s!r} is not 'none', 'fake', an epoch or a UTC date-time")


def child_env(config_home: str | None, tz: str | None, clock: float | None, cwd: str,
              cols: int, rows: int) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k in ("COLORTERM", "FORCE_COLOR", "NO_COLOR", "TERM_PROGRAM",
                         "TERM_PROGRAM_VERSION", "TERM_SESSION_ID", "TMUX", "STY")
                   or k.startswith(("TEXTUAL", "ITERM", "KITTY", "WEZTERM", "VTE_")))}
    env.update(TERM=TERM, PROMPT_TOOLKIT_NO_CPR="1", COLUMNS=str(cols), LINES=str(rows),
               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    env.setdefault("LANG", "en_US.UTF-8")
    if not env.get("LC_ALL", "").upper().endswith(("UTF-8", "UTF8")) and env.get("LC_ALL"):
        env["LC_ALL"] = "en_US.UTF-8"
    if config_home:
        env["XDG_CONFIG_HOME"] = config_home
    if tz:
        env["TZ"] = tz
    if clock is not None:
        env["TUI_DRIVE_CLOCK"] = repr(clock)
    return env


def secrets_in(config_home: str | None) -> list[str]:
    """The saved credential strings, only to scrub them from anything printed."""
    base = config_home or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    try:
        d = json.loads((Path(base) / "jav3" / "credentials.json").read_text())
    except (OSError, ValueError):
        return []
    return [str(d[k]) for k in ("token", "session") if isinstance(d.get(k), str) and len(d[k]) > 8]


def child_main(argv: list[str]) -> None:
    """`tui_drive.py --_client CLI ARGS...`: run the client as a script with time.time()
    pinned (TUI_DRIVE_CLOCK), so ages, dates and the home page's rotating example do not
    depend on when the screen is taken."""
    import importlib.machinery
    import importlib.util
    cli, args = argv[0], argv[1:]
    if os.environ.get("TZ"):
        time.tzset()
    loader = importlib.machinery.SourceFileLoader("jav3_driven", cli)
    spec = importlib.util.spec_from_loader("jav3_driven", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)           # defines everything; its __main__ guard stays off
    clock = os.environ.get("TUI_DRIVE_CLOCK")
    if clock:
        fixed = float(clock)

        class FixedClock:
            """The client's `time` module, with time() fixed. asyncio and Textual keep
            the real one (they use monotonic()), so nothing waits on a frozen clock."""
            time = staticmethod(lambda: fixed)

            def __getattr__(self, name):
                return getattr(time, name)

        mod.time = FixedClock()
    sys.exit(mod.main(args))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tui_drive.py", description="Drive the real jav3 terminal client in a pty.",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="?", default="show", help='"type:/vms|key:enter|wait:1|show"')
    ap.add_argument("--fake", action="store_true",
                    help="serve tests/tui_fake_http.py's seeded fake server (no network)")
    ap.add_argument("--server", help="pi (= %s), or an address/URL for the client's --server; "
                    "uses your saved login" % PI)
    ap.add_argument("--size", type=parse_size, default=(120, 40), metavar="COLSxROWS")
    ap.add_argument("--timeout", type=float, default=10.0, help="default waitfor timeout (s)")
    ap.add_argument("--args", default="", help='extra client arguments, e.g. "-r 4"')
    ap.add_argument("--clock", default=None,
                    help="pin the client's time.time(): none | fake | epoch | 2026-09-29T12:00:00 "
                         "(default: fake with --fake, else none)")
    ap.add_argument("--tz", default=None, help="TZ for the client (default UTC with --fake)")
    ap.add_argument("--config", help="XDG_CONFIG_HOME for the client (default: yours; a "
                    "temp dir with --fake)")
    ap.add_argument("--cli", default=str(CLI), help="the client script (default: this repo's)")
    ap.add_argument("--python", default=sys.executable, help="interpreter for the client")
    ap.add_argument("--startup", type=float, default=15.0,
                    help="seconds to wait for the first frame")
    ap.add_argument("--quiet", action="store_true", help="no trailing status line")
    a = ap.parse_args(argv)
    cols, rows = a.size
    import shlex
    steps = split_steps(a.steps)

    tmp = tempfile.mkdtemp(prefix="tui-drive-")
    fake = http = None
    config = a.config
    client_args = shlex.split(a.args)
    clock_default = None
    try:
        if a.fake:
            sys.path.insert(0, str(REPO / "tests"))
            import tui_fake_http as tf
            fake = tf.SeededServer()
            http = tf.HttpFake(fake.handle).start()
            config = str(Path(tmp) / "config")
            cdir = Path(config) / "jav3"
            cdir.mkdir(parents=True, mode=0o700)
            (cdir / "credentials.json").write_text(json.dumps(
                {"address": http.address, "session": "fake-session", "username": tf.USERNAME}))
            clock_default, tz = tf.CLOCK, a.tz or tf.ZONE
        else:
            tz = a.tz
            if a.server:
                client_args = ["--server", PI if a.server == "pi" else a.server] + client_args
                print(f"-- {a.server}: using your saved login", file=sys.stderr)
        clock = parse_clock(a.clock, clock_default) if a.clock else clock_default
        cwd = str(Path(tmp) / "work")
        os.mkdir(cwd)
        env = child_env(config, tz, clock, cwd, cols, rows)
        argv_child = [a.python, str(Path(__file__).resolve()), "--_client", a.cli] + client_args
        sess = Session(argv_child, env, cols, rows, cwd, redact=secrets_in(config))
        run = Runner(sess, a.timeout)
        code = 0
        try:
            sess.settle(quiet=0.5, cap=a.startup, first=True)
            if not sess.alive:
                run.emit(run.screen_dump(f"the client exited at startup (status {sess.status})"))
                return 1
            run.run(steps)
            code = 1 if run.failed else 0
        except StepError as e:
            print(f"tui_drive: {e}", file=sys.stderr)
            code = 2
        finally:
            sess.pump(0.0)
            died = not sess.alive
            sess.close()
        if not a.quiet:
            extra = ""
            if fake is not None and fake.misses:
                extra = "; fake 404: " + ", ".join(sorted(set(fake.misses))[:6])
            print(f"-- done: exit {code}{'; client had exited (status %s)' % sess.status if died else ''}"
                  f"{extra}")
        return code
    finally:
        if http:
            http.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--_client":
        child_main(sys.argv[2:])
    else:
        sys.exit(main())
