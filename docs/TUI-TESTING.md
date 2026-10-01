# Testing the terminal client

The client is `clients/jav3cli/jav3` (one file, Textual). The operator's terminal is macOS
Terminal.app: **256 colours** (a blended colour rounds to grey, so accents are solid theme
colours) and **no shift+arrows** (every key route needs a plain key).

## For the operator

- Install: `curl -fsSL http://10.0.0.82:8000/cli/install.sh | sh`, then `jav3 login` (paste the
  line from Settings → Add computer; chat only) or `jav3 login --password` (full access: /vms,
  /security). `jav3 --server 10.0.0.82:8000` points one run at another server with the saved login.
  An installed copy updates itself: `/update` (or `jav3 update`) after the notice
  "a newer jav3 is on the server"; a checkout says `git pull` instead.
- Pages (jav3.2): `/agents [finished]`, `/security [tab]`, `/vms [tab]` open over the chat with the
  prompt left as one line under them. On a page `/` jumps to that line (type `/vms images`, enter),
  `esc` goes back to the page before (the chat first), `ctrl+c` or `/work` straight to the chat,
  and `ctrl+x` plus a letter works there as everywhere. Pages the web has and the terminal does
  not (`/memory /settings /logs /schedules /tools /artifacts`) answer "not in the terminal yet";
  `/web memory` opens that page in the browser.
- Panels (jav3.2): the area above the prompt tiles like tmux, up to four panels. `/new-panel`
  splits the focused panel along its longer side with a new chat; a trailing `panel` on a page
  command (`/vms images panel`, `/sessions 7 panel`, `/work panel`) opens it in a new panel. Each
  panel has its own chat (several turns stream at once) and its own back stack; its border title
  says its number, what it shows and `● running` / `needs input`. One prompt line under all of
  them types into the focused panel's chat (the placeholder names it) and runs `/page` commands
  from anywhere. Keys: `ctrl+x` then `←↑↓→` moves to the neighbouring panel, `1`-`4` jumps, `z`
  zooms the focused panel and back, `0` closes it (its chat keeps running on the server; the
  last panel cannot be closed); on an agents row `p` opens that chat in a new panel. The sidebar
  is hidden while there are two or more panels. A dialog for a turn in another panel opens over
  everything with the panel number in its title. At 80x24 three panels fit (two side by side, one
  split across); a split that would leave a tile under 28x8 says so and refuses.
- What to try, per page (a build is fine if each of these does what it says):
  - Home: `/help` lists the keys; `ctrl+p` palette. `←` / `→` on the empty prompt open the agents /
    sessions DRAWER over the panels (with text in the prompt they move the cursor): up/down move,
    `enter` opens the entry in the focused panel, `p` in a new panel (not at four), `esc` closes;
    in the agents drawer `→` / `←` go into / out of an entry's sub-agents (`←` at the top, `→` in
    the sessions drawer, closes). Each lists what runs or needs you first, then a few recent.
  - Chat: send a message; tool rows show on one line, enter on a picked row opens it; `ctrl+c`
    stops the turn; `shift+tab` cycles permissions; `ctrl+x l` lists chats, `jav3 -c` resumes.
  - `/vms`: tabs Boxes, Images, Catalogue (`tab`, `1`-`3`); up/down picks a box, `enter` shows its
    history, `s` `r` `x` `d` `c` ask before they act; the rows refresh by themselves.
  - `/security`: Queue first (y approve, n deny, a acknowledge); `1`-`8` jump to Network, Logs,
    Secrets, Persistent, Profiles, Rules, Calls. `esc` goes back.
  - `/agents` (the full page; `←` is its drawer): up/down, `→` into an entry's agents, `←` out,
    `enter` opens its chat; `/agents finished` opens the second view.
- Look for: text you cannot read (grey on grey), a line cut mid-word at 80 columns, a key that
  needs shift, and a page that shows "no ..." when the server has data.

## For agents

Drive the real client in ONE command (pexpect pty + pyte screen, TERM=xterm-256color, no
COLORTERM). Steps are joined with `|` (`\|` is a literal pipe):

    scripts/tui_drive.py --fake "type:/vms|key:enter|waitfor:RAM@10|show"
    scripts/tui_drive.py --fake --size 80x24 "key:left|waitfor:RUNNING|show|bg:"   # the agents drawer
    scripts/tui_drive.py --fake --args "-r 4" "waitfor:Not checked|fg:|snap:/tmp/chat.txt"
    scripts/tui_drive.py --server pi "key:right|wait:2|show"    # the Pi: the sessions drawer

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
(`/agents`, `/agents finished`), the two drawers (`drawer-agents`, `drawer-sessions`, and `-2` with two
panels behind), `/security calls`, `/vms images`, a web-only page (`/memory`), two and three
chat panels and a page in a new panel (`panels-2`, `panels-3`, `panels-page`)
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

Panels: `tests/test_cli_panels.py` has the pure split-tree tests (split, close, normalize, rects,
neighbour, the cut direction) and the pilot tests (focus routing, two chats streaming through two
FakeServer feeds, `panel` and `/new-panel`, the cap, ctrl+x keys, a dialog for an unfocused panel,
closing a panel). In a test `app.chats` maps a panel number to its `ChatState` (`.panel`, `.log`,
`.cid`, `.turn`, `.busy`); `app.cid`, `app.turn`, `app.busy`... are the focused panel's (the chat
in context: a worker keeps the chat that started it). `app.focus_panel(n)` moves the focus,
`app.panel_area.size` is the room the tiles share. Driven: `scripts/tui_drive.py --fake
"type:/new-panel|key:enter|waitfor:2 chat (new)@10|key:ctrl+x|key:left|show"` (`key:ctrl+x` then
`key:z`, `key:0`, `key:1`...), and `fg:╔` / `fg:╭` give the focused and unfocused border colours.
