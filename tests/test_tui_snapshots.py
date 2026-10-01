"""Per-page screen snapshots of the real terminal client, the way macOS Terminal.app shows
it: a pty, TERM=xterm-256color (no COLORTERM, so no truecolor), 80x24 and 160x48.

Each test starts clients/jav3cli/jav3 unmodified against tests/tui_fake_http.py's seeded
fake (no network), on a pinned clock and time zone, opens one page, and compares the
screen to tests/tui_snapshots/<page>-<cols>x<rows>.txt: the text, then one line per row of
colour runs (fg/bg as xterm-256 indexes), so a contrast or accent change fails too. A
failure prints the diff. After an intended change:

    TUI_SNAPSHOT_UPDATE=1 python -m pytest tests/test_tui_snapshots.py

and look at `git diff tests/tui_snapshots` before committing it.

Why pexpect + pyte and not Textual's run_test(size=...): run_test is headless with its own
colour system, so it cannot tell a 256-colour terminal's rounding from truecolor, and it
never emits the escape codes Terminal.app receives. Here the client picks its colours from
TERM like it does for the operator, and a truecolor code in the output fails the test.
(~3 s per page, one process each; the 10 run in parallel under -n.)
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

# page -> (client args, steps). Each waits for what proves the page loaded.
PAGES = {
    "home": ([], "waitfor:full access"),
    "chat": (["-r", "4"], "waitfor:Not checked"),
    "vms": ([], "type:/vms|key:enter|waitfor:RAM 1280|waitfor:shared kvm"),
    "security": ([], "type:/security|key:enter|waitfor:Secrets 2|waitfor:registry.npmjs.org"),
    "help": ([], "type:/help|key:enter|waitfor:Keys"),
    # TODO: "agents" (the left-arrow screen). Its layout is being changed; add it
    # as ([], "key:left|waitfor:Agents") once that lands.
}


def normalise(text: str) -> str:
    """What differs per run, at the same width: the fake's free port (the home page shows
    it) and the throw-away config dir (/help shows where themes go)."""
    text = re.sub(r"127\.0\.0\.1:\d{5}", "127.0.0.1:NNNNN", text)
    return re.sub(r"/tmp/tui-\w{8}/", "/tmp/tui-XXXXXXXX/", text)


def capture(page: str, cols: int, rows: int) -> str:
    args, steps = PAGES[page]
    with td.driven(fake=True, size=(cols, rows), client_args=args, timeout=15) as d:
        assert d.sess.alive, "the client exited at startup"
        d.run.run(td.split_steps(steps))
        assert not d.run.failed, d.run.screen_dump("a wait timed out")
        d.sess.settle(quiet=0.3, cap=1.0)
        shot = td.snapshot_text(d.screen, f"{page}")
        tc = td.truecolor_cells(d.screen)
        assert not tc, f"truecolor reached a 256-colour terminal: {tc[:5]}"
        assert not d.fake.misses, f"the client asked for routes the fake lacks: {d.fake.misses}"
    return normalise(shot)


pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")


@pytest.mark.parametrize("cols,rows", SIZES, ids=[f"{c}x{r}" for c, r in SIZES])
@pytest.mark.parametrize("page", list(PAGES))
def test_page_snapshot(page, cols, rows):
    shot = capture(page, cols, rows)
    path = SNAPS / f"{page}-{cols}x{rows}.txt"
    if UPDATE:
        SNAPS.mkdir(exist_ok=True)
        path.write_text(shot)
        return
    if not path.exists():
        pytest.fail(f"no snapshot {path.name}: run with TUI_SNAPSHOT_UPDATE=1 to create it")
    want = path.read_text()
    if shot != want:
        diff = "\n".join(difflib.unified_diff(
            want.splitlines(), shot.splitlines(), f"{path.name} (committed)", "now",
            lineterm="", n=2))
        pytest.fail(f"{page} at {cols}x{rows} changed; TUI_SNAPSHOT_UPDATE=1 accepts it:\n"
                    + diff[:6000])


def test_a_live_turn_streams_to_the_end():
    """Not a snapshot: typing a message runs the fake's scripted turn through the real
    stream reader, and the tool rows and the reply land on screen."""
    with td.driven(fake=True, size=(100, 30), timeout=15) as d:
        d.run.run(td.split_steps("type:hello|key:enter|waitfor:Not checked@15|wait:0.3"))
        assert not d.run.failed, d.run.screen_dump("stuck")
        text = d.sess.text()
        assert "Grep \"def retry\"" in text and "Edit src/sync/retry.py" in text
        assert d.fake.posts and d.fake.posts[0]["message"] == "hello"
