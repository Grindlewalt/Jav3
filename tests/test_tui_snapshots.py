"""Per-page screen snapshots of the real terminal client, the way macOS Terminal.app shows
it: a pty, TERM=xterm-256color (no COLORTERM, so no truecolor), 80x24 and 160x48.

Each test starts clients/jav3cli/jav3 unmodified against tests/tui_fake_http.py's seeded
fake (no network), on a pinned clock and time zone, opens one page, and compares the
screen to tests/tui_snapshots/<page>-<cols>x<rows>.txt: the text, then one line per row of
colour runs (fg/bg as xterm-256 indexes), so a contrast or accent change fails too. A
failure prints the diff. After an intended change:

    TUI_SNAPSHOT_UPDATE=1 python -m pytest tests/test_tui_snapshots.py

and look at `git diff tests/tui_snapshots` before committing it.

Why pexpect + pyte and not Textual's run_test(size=...): run_test builds the app on a
truecolor console whatever TERM says (app.truecolor is True), so the client would draw its
truecolor themes; TEXTUAL_COLOR_SYSTEM=256 fixes that and starts in 0.5 s instead of ~2.5 s,
but it still reports Rich's colours before they are rounded to the 256 indexes the terminal
receives, which is where grey-on-grey and blended accents show up. Here the client picks its
colours from TERM like it does for the operator, the snapshot holds the indexes, and a
truecolor code in the output fails the test. (~2 s per page, one process each: ~23 s serial,
~8 s under -n 6.)
"""
import difflib
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("pexpect")
pytest.importorskip("pyte")
pytest.importorskip("textual")
pytest.importorskip("httpx")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))
import tui_drive as td  # noqa: E402

SNAPS = HERE / "tui_snapshots"
UPDATE = bool(os.environ.get("TUI_SNAPSHOT_UPDATE"))
SIZES = [(80, 24), (160, 48)]
CLI = os.environ.get("JAV3_CLIENT") or str(td.CLI)     # same switch as cli_fake.load_client

# page -> (client args, steps). Each waits for what proves the page loaded.
PAGES = {
    "home": ([], "waitfor:full access"),
    "chat": (["-r", "4"], "waitfor:Not checked"),
    "vms": ([], "type:/vms|key:enter|waitfor:RAM 1280|waitfor:shared kvm"),
    "security": ([], "type:/security|key:enter|waitfor:Secrets 2|waitfor:registry.npmjs.org"),
    "help": ([], "type:/help|key:enter|waitfor:Keys"),
    # the left-arrow screen: the first root selected (and unfolded), then an agent inside
    # a plan (its failed item), then the Finished view
    # pages by slash command with an argument (jav3.2 P0): the page, its tab, its chrome
    "security-calls": ([], "type:/security calls|wait:0.4|key:enter|waitfor:CALL@10|wait:0.5"),
    "vms-images": ([], "type:/vms images|wait:0.4|key:enter|waitfor:build a new version@10"
                       "|wait:0.5"),
    "agents-cmd-finished": ([], "type:/agents finished|wait:0.4|key:enter|waitfor:homelab@10"
                                "|wait:0.5"),
    # a page the web has and the terminal does not: the answer where it lives
    "stub": ([], "type:/memory|wait:0.4|key:enter|waitfor:not in the terminal yet@10|wait:0.3"),
    # panels (jav3.2 P1): two chats side by side, three (the second split across), and a
    # page in a new panel beside the chat
    "panels-2": ([], "type:/new-panel|wait:0.4|key:enter|waitfor:2 chat (new)@10|wait:0.5"),
    "panels-3": ([], "type:/new-panel|wait:0.4|key:enter|waitfor:2 chat (new)@10"
                     "|type:/new-panel|wait:0.4|key:enter|waitfor:3 chat (new)@10|wait:0.5"),
    "panels-page": ([], "type:/vms panel|wait:0.4|key:enter|waitfor:RAM 1280@10"
                        "|waitfor:shared kvm|wait:0.5"),
    "agents": ([], "key:left|waitfor:Agents@10|waitfor:morning quotes|wait:0.3"),
    "agents-in": ([], "key:left|waitfor:Agents@10|waitfor:morning quotes|key:right|key:down*2"
                      "|wait:0.3"),
    "agents-finished": ([], "key:left|waitfor:Agents@10|waitfor:morning quotes|key:down*3"
                            "|key:enter|waitfor:homelab|wait:0.3"),
}


def normalise(text: str) -> str:
    """What differs per run, at the same width: the fake's free port (the home page shows
    it), the throw-away config dir (/help shows where themes go) and the spinner frame."""
    text = re.sub(r"127\.0\.0\.1:\d{5}", "127.0.0.1:NNNNN", text)
    # a spinner frame depends on when the screen was read: every frame reads as the first
    text = re.sub("[\u2800-\u28ff]", "\u280b", text)
    return re.sub(r"/tmp/tui-\w{8}/", "/tmp/tui-XXXXXXXX/", text)


# Text under 3:1 contrast in the 256-colour palette is a bug (grey on the teal selection once
# was). /help is left out: its backdrop is the page behind it, dimmed on purpose.
CONTRAST_PAGES = ("home", "chat", "vms", "security", "agents", "agents-in", "agents-finished",
                  "security-calls", "vms-images", "agents-cmd-finished", "stub", "panels-2",
                  "panels-3", "panels-page")


def capture(page: str, cols: int, rows: int) -> tuple[str, list[str]]:
    """(the snapshot text, the unreadable runs)"""
    args, steps = PAGES[page]
    with td.driven(fake=True, size=(cols, rows), client_args=args, timeout=15, cli=CLI) as d:
        assert d.sess.alive, "the client exited at startup"
        d.run.run(td.split_steps(steps))
        assert not d.run.failed, d.run.screen_dump("a wait timed out")
        d.sess.settle(quiet=0.3, cap=1.0)
        shot = td.snapshot_text(d.screen, f"{page}")
        tc = td.truecolor_cells(d.screen)
        assert not tc, f"truecolor reached a 256-colour terminal: {tc[:5]}"
        assert not d.fake.misses, f"the client asked for routes the fake lacks: {d.fake.misses}"
        low = [f"row {y} cols {x0}-{x1} {td.style_str(st)} ({td.style_contrast(st):.1f}:1) {txt.strip()[:40]!r}"
               for y, x0, x1, txt, st in td.text_runs(d.screen) if td.style_contrast(st) < td.LOW]
    return normalise(shot), low


pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")


@pytest.mark.parametrize("cols,rows", SIZES, ids=[f"{c}x{r}" for c, r in SIZES])
@pytest.mark.parametrize("page", list(PAGES))
def test_page_snapshot(page, cols, rows):
    shot, low = capture(page, cols, rows)
    path = SNAPS / f"{page}-{cols}x{rows}.txt"
    if UPDATE:
        SNAPS.mkdir(exist_ok=True)
        path.write_text(shot)
    elif not path.exists():
        pytest.fail(f"no snapshot {path.name}: run with TUI_SNAPSHOT_UPDATE=1 to create it")
    want = path.read_text() if not UPDATE else shot
    if shot != want:
        diff = "\n".join(difflib.unified_diff(
            want.splitlines(), shot.splitlines(), f"{path.name} (committed)", "now",
            lineterm="", n=2))
        pytest.fail(f"{page} at {cols}x{rows} changed; TUI_SNAPSHOT_UPDATE=1 accepts it:\n"
                    + diff[:6000])
    if page in CONTRAST_PAGES and low:
        pytest.fail(f"{page} at {cols}x{rows}: text under 3:1 contrast in 256 colours:\n"
                    + "\n".join(low[:12]))


def test_a_live_turn_streams_to_the_end():
    """Not a snapshot: typing a message runs the fake's scripted turn through the real
    stream reader, and the tool rows and the reply land on screen."""
    with td.driven(fake=True, size=(100, 30), timeout=15, cli=CLI) as d:
        d.run.run(td.split_steps("type:hello|key:enter|waitfor:Not checked@15|wait:0.3"))
        assert not d.run.failed, d.run.screen_dump("stuck")
        text = d.sess.text()
        assert "Grep \"def retry\"" in text and "Edit src/sync/retry.py" in text
        assert d.fake.posts and d.fake.posts[0]["message"] == "hello"
