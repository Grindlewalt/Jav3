# Testing the terminal client

The client is `clients/jav3cli/jav3` (one file, Textual). The operator's terminal is macOS
Terminal.app: **256 colours** (a blended colour rounds to grey, so accents are solid theme
colours) and **no shift+arrows** (every key route needs a plain key).

## For the operator

- Install: `curl -fsSL http://10.0.0.82:8000/cli/install.sh | sh`, then `jav3 login` (paste the
  line from Settings → Add computer; chat only) or `jav3 login --password` (full access: /vms,
  /security). `jav3 --server 10.0.0.82:8000` points one run at another server with the saved login.
- Pages (jav3.2): `/agents [finished]`, `/security [tab]`, `/vms [tab]` open over the chat with the
  prompt left as one line under them. On a page `/` jumps to that line (type `/vms images`, enter),
  `esc` goes back to the page before (the chat first), `ctrl+c` or `/work` straight to the chat,
  and `ctrl+x` plus a letter works there as everywhere. A trailing `panel` (`/vms images panel`) is
  understood but panels come in the next step: for now it opens in place and says so. Pages the web
  has and the terminal does not (`/memory /settings /logs /schedules /tools /artifacts`) answer
  "not in the terminal yet"; `/web memory` opens that page in the browser.
- What to try, per page (a build is fine if each of these does what it says):
  - Home: `/help` lists the keys; `←` on the empty prompt opens agents; `ctrl+p` palette.
  - Chat: send a message; tool rows show on one line, enter on a picked row opens it; `ctrl+c`
    stops the turn; `shift+tab` cycles permissions; `ctrl+x l` lists chats, `jav3 -c` resumes.
  - `/vms`: tabs Boxes, Images, Catalogue (`tab`, `1`-`3`); up/down picks a box, `enter` shows its
    history, `s` `r` `x` `d` `c` ask before they act; the rows refresh by themselves.
  - `/security`: Queue first (y approve, n deny, a acknowledge); `1`-`8` jump to Network, Logs,
    Secrets, Persistent, Profiles, Rules, Calls. `esc` goes back.
  - `/agents` (or `←` on the empty prompt): up/down, `→` into an entry's agents, `←` out, `enter`
    opens its chat; `/agents finished` opens the second view.
- Look for: text you cannot read (grey on grey), a line cut mid-word at 80 columns, a key that
  needs shift, and a page that shows "no ..." when the server has data.

## For agents

Drive the real client in ONE command (pexpect pty + pyte screen, TERM=xterm-256color, no
COLORTERM). Steps are joined with `|` (`\|` is a literal pipe):

    scripts/tui_drive.py --fake "type:/vms|key:enter|waitfor:RAM@10|show"
    scripts/tui_drive.py --fake --size 80x24 "key:left|waitfor:Agents|show|bg:"
    scripts/tui_drive.py --fake --args "-r 4" "waitfor:Not checked|fg:|snap:/tmp/chat.txt"
    scripts/tui_drive.py --server pi "key:left|wait:2|show"     # the Pi, your saved login

Use `<venv>/bin/python scripts/tui_drive.py` (needs pexpect, pyte, textual, httpx). Steps: `type:`
`paste:` `key:` (`enter escape tab shift+tab up down left right home end pageup pagedown backspace
ctrl+x alt+x f1`, `down*3`, `down,down,enter`) `raw:` `wait:S` `waitfor:TEXT@S` `waitgone:` `expect:`
`absent:` `show` `bg:` `fg:[TEXT]` `snap:FILE` `resize:WxH`. A `waitfor`/`expect` that fails prints
the screen and exits 1; `--timeout`, `--clock`, `--tz`, `--startup` tune it. Output is the screen
as text (trailing blank rows dropped). `bg:` prints each row's dominant fg/bg as xterm-256 indexes
and flags runs under 3:1 contrast (`LOW`); `fg:TEXT` gives the colours where TEXT is drawn, `fg:`
every fg/bg pair, worst first. A trailing line lists routes the fake answered 404.

`--fake` serves `tests/tui_fake_http.py`: `cli_fake.FakeServer` (the Textual-pilot tests' fake) plus
seeded data over real HTTP, on a pinned clock (2026-09-29 12:00 UTC) and zone, with a throw-away
config dir (your login is never read). Seeded: chats 1-4 (#4 finished with tool calls; #3 running,
`-r 3` attaches), `/vms` boxes, images, catalogue and a leftover, `/security` rows on every tab,
agents for the left arrow. Messages: "slow" runs a tool until you stop it, "error" fails, "ask"
asks you a question; anything else plays a short turn. Writes (approve, restart ...) answer 200
and change nothing; `Driven.fake.writes` lists them. For a new page, add its routes to
`SeededServer.seeded` (a 404 means the page rendered empty).

Against the Pi: read-only. Open pages; do not send chat messages or press approve/deny/destroy.

### Snapshots

`tests/test_tui_snapshots.py` opens home, a finished chat, /vms, /security, /help, the agents page
(by `←` and by `/agents finished`), `/security calls`, `/vms images` and a web-only page (`/memory`)
at 80x24 and 160x48 and compares `tests/tui_snapshots/<page>-<cols>x<rows>.txt` (the text, then fg/bg runs per
row as xterm-256 indexes). It also fails on any truecolor code. ~25 s alone, ~8 s under `-n 6`.
It skips without pexpect, pyte or textual. After an intended UI change:

    TUI_SNAPSHOT_UPDATE=1 <venv>/bin/python -m pytest tests/test_tui_snapshots.py
    git diff tests/tui_snapshots     # read it: every changed line is a change you meant

Why pexpect+pyte, not Textual's `run_test(size=...)`: the client picks its colours from TERM, so
only a real pty shows what Terminal.app gets (256-colour rounding, no truecolor). The driver
pins the client's `time.time()` and stops the cursor blinking (`TUI_DRIVE_STEADY`) without
touching the client. Not covered: live spinners and durations (the live-turn test asserts text only).

Pages are widgets in a `PageHost` (see the comment above `class Page` in the client): a page's keys
work while it has focus, so a driven page needs no extra step after its command. The prompt under a
page is a command line: `/` on a page moves the focus to it at once, so `type:/vms images|key:enter`
works from a page too. `tests/test_cli_pages.py` is the pilot-level
test of the router and the back stack; `app.top` is the dialog over everything, else the page.
