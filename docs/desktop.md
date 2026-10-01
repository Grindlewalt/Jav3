# The live desktop of a box

A box on the `desktop` image can show its screen in the web app: a Work window
(`+` menu, `/desktop`, or `/window desktop`) that watches it live. Three steps:
P1 **watch only** (the window), P2 **the agent drives it** with the desk tool
(below), P3 **the operator clicks in to take over and hands back** (below).

KVM only. The desktop is an image layer (`vm/images/desktop.recipe`); there is no
Docker image of it, and a Docker box says so.

## What runs where

    browser (noVNC, view only)
      -- WebSocket /api/vm/boxes/{id}/display/ws  (cookie, same-origin, subprotocol `binary`)
    host   backend/vm/display_api.py   pins the box, filters the client bytes, splices
      -- vsock :5559 (box.json listen.display; boxes.PORT_DISPLAY)
    guest  guest/backend/display.py    Xvnc :100 1280x800 + openbox + an xterm on tmux `desk`
      -- unix socket /run/jav3-display.sock (never TCP)

    agent  desk(action=..., computer="sandbox")  -> backend/desk.py act()
      -- backend/vm/boxdesk.py   a shim `ws`: JSON lines over vsock :5559 {"mode":"desk"}
      -- guest/backend/display.py  -> child: guest/backend/deskbox.py  (jav3-desk's Session)
      -- xdotool / maim on DISPLAY :100

- **Start is explicit.** `GET /api/vm/boxes/{id}/display` only reads (supported?,
  session state, `need_mb` / `free_mb`). `POST` is the operator's button: it boots
  the box if it is stopped, but refuses with the arithmetic ("the desktop box needs
  1424 MB, 1034 MB free") when the RAM budget has no room, then starts the screen.
  The WebSocket never boots anything: a stopped box is refused.
- **Free RAM** is the budget cap less the cost of the other RUNNING boxes.
- **The screen is lazy.** Xvnc starts when the first viewer (or the POST) asks, and
  stops 5 minutes after the last viewer leaves (`display.IDLE_STOP_S`). It is
  1280x800 on purpose: the agent's own screenshots are that size. Every process
  runs under `memguard.confine`, so when memory runs out the desktop dies, not the
  run-turn server. The tmux session `desk` outlives the screen.
- **A window is a view until it takes control, and that is enforced on the server.**
  Every client byte goes through `display_api.RfbInputFilter`, which parses the RFB
  stream and drops KeyEvent, PointerEvent, ClientCutText, QEMU extended key events,
  SetDesktopSize and xvp unless the `allow` callback says yes (key and pointer for the
  holder of control only, see "The operator takes over"; never the clipboard, resize
  or xvp); an unknown message type ends the session. Xvnc also has the clipboard off
  both ways and ignores resize requests.
- State goes through the shared stream (topic `vm-boxes`): `{"type":"display",
  "box_id", "viewers"|"state"|"control"|"agent"}`. The window never opens its own
  EventSource.

## Guest protocol (port 5559)

One JSON line in, one JSON line out, then (for `rfb`) raw RFB bytes:
`{"mode":"rfb"}` starts the screen if needed, answers `{"ok":true}`, splices;
`start`, `status`, `stop` answer the status; `desk` (P2, below) answers `{"ok":true}`
and then speaks JSON lines. Errors are `{"ok":false,"error":...}`.
When the display starts it writes `/run/jav3-desk-apps.json` (`display.apps()`):
`terminal` (xterm on tmux `desk`) and `browser` (chromium with
`--disable-background-networking --disable-sync --no-first-run
--proxy-server=<the box's proxy> --no-sandbox`, a throwaway profile). The terminal
is opened with the screen so there is something to watch.

## The agent drives it (P2)

The agent uses the SAME `desk` tool it uses on the operator's computer (no new tool
name): `desk(action="screenshot" | "click" | "type" | "key" | "scroll" | "drag" |
"open" | "wait", computer="sandbox")`. The model calls a box's desktop `sandbox`.
`backend/vm/boxdesk.py` makes the box look to `backend/desk.py` like a computer that
connected, so everything the desk path already does applies unchanged: grants, the
fresh-screenshot-before-input rule, frame serials and stale ids, rate limits, the
stuck-click note, the `desk_actions` audit (typed text as length + sha256), security
events, and the taint (a screen is untrusted text).

    desk(action="screenshot", computer="sandbox")
      -> screenshot 1280x800 attached, frame 1,
         "(no elements: the box's desktop has no accessibility tree — click by coordinates)"
    desk(action="open", app="terminal", computer="sandbox")   # an xterm on tmux `desk`
    desk(action="click", x=400, y=300, computer="sandbox")    # coordinates of the latest frame
    desk(action="type", text="jav3 --server 127.0.0.1:8099", computer="sandbox")
    desk(action="key", combo="Return", computer="sandbox")
      -> "key done", changed: yes, the new screenshot

- **Registration.** `boxdesk.ensure(box)` dials the guest (`{"mode":"desk"}`), waits for
  its hello and calls `desk.attach(box_id=...)`. It runs when the operator starts the
  desktop (`POST .../display`) and whenever a status read finds the screen up with no
  desk registered (`boxdesk.sync`), so a host restart heals on the next look. The
  connection lives exactly as long as the screen: when the display stops (idle, Stop,
  box down) the guest ends it, the desk detaches and the tools disappear. The agent
  never starts the screen: that is the operator's RAM decision.
- **Identity and grants.** A box desk has a `device_tokens` row: scope `desk`, name
  `box:<box id>`, `paired_by = 'box'`, its secret thrown away at creation (nothing can
  connect with it), no expiry. So it appears in Settings -> Access -> Computer use with
  the Stop button and the audit tail. Defaults on first registration: **screen on,
  input on, shell off** (`run_code` is the box's shell). Later registrations never
  touch the row's grants: a Stop (everything off) sticks until the operator turns them
  back on.
- **Scope.** `Desk.box_id`. `desk.resolve()` / `offered()` / `shell_offered()` go through
  `desk._pool(box)`: a turn running in a box with a registered desktop sees ONLY that
  desktop (never the operator's Mac), and a turn anywhere else never sees a box
  desktop. The box of a turn is the host's own binding (`boxes.op_box(op_id)`, set by
  `guest_turn`), never the guest's claim; while the tools are being listed, before the
  turn is bound, its project's own box (`p-<slug>`) stands in. A box that had a desktop
  this run keeps hiding the Mac after its screen stops (the turn is told the desktop is
  not running).
- **Guest.** `display._handle` mode `desk` needs the screen up (it never starts it),
  spawns `python3 -m backend.deskbox` as a child under `memguard.confine` with
  `DISPLAY=:100`, and pipes the host's connection to its stdin/stdout. The child is
  the operator's `clients/jav3-desk/jav3-desk` (shipped as `backend/jav3_desk.py`) running
  its own `Session.pump` over those pipes on an X11 backend (xdotool for input, maim
  for screenshots, Pillow for the settle check). Its apps are the ones in
  `/run/jav3-desk-apps.json` (`terminal`, `browser`); `open url=` goes to the box's
  browser. There is no accessibility tree in the box, so a screenshot has no element
  list: the agent clicks by coordinates (or `target=` when a grounding model is set).
- **Hold.** The display stays up while the agent works: the first request takes
  `Session.hold()`, every request pushes the release back `HOLD_GRACE_S` (60 s), and the
  hold is dropped at once when the connection ends. After that the 5 minute idle clock
  runs as before (it does not run while someone watches).
- **Exact terminal text.** The xterm is attached to tmux session `desk`, so through
  `run_code` the agent can type with `tmux send-keys -t desk '...' Enter` and read the
  screen exactly with `tmux capture-pane -p -t desk` (no OCR). The desk playbook says so.

### Trying the jav3 terminal client inside the box

The box cannot reach the real Jav3 server, on purpose and for good. The agent runs the
client against the test server in the pushed project workspace (the Jav3 repo as the
project), both baked into the desktop image's `/opt/jav3/py` venv (`httpx`, `textual`):

    # run_code, from the workspace root; in the background, output redirected
    python tests/tui_fake_http.py 8099 > /tmp/fake.log 2>&1 &
    cat /tmp/fake.log     # prints  XDG_CONFIG_HOME=/tmp/tui-fake-... python clients/jav3cli/jav3
    # then, in the desktop terminal (desk type + key Return, or tmux send-keys -t desk):
    XDG_CONFIG_HOME=/tmp/tui-fake-... python clients/jav3cli/jav3

The fake serves seeded chats, a running turn, boxes and security rows; `jav3 --server
127.0.0.1:8099` names it too. The screenshot shows the TUI; `tmux capture-pane` reads it.

## The operator takes over (P3)

Control is a field on the box's desktop: `agent` (the default) or `operator`. The state
lives in `backend/vm/boxdesk.py` (`take`, `hand_back`, `admit`, `control_state`); the
routes and the socket live in `backend/vm/display_api.py`.

    window header:  ● agent driving  |  YOU have control: agent paused 00:41  |  watching
    above the screen:  Agent is driving · click the screen to take over   [Stop]
    below the screen:  YOU have control: agent paused 00:41               [Hand back]

- **Taking.** The first click (or key) on the screen calls
  `POST /api/vm/boxes/{id}/display/control {"holder":"operator","viewer":<id>}`. The click
  is consumed (it is not sent to the box). `viewer` is the id the window gave its own
  noVNC session (`?viewer=` on the WebSocket, random per window); the server refuses an id
  that is not connected (409), and a second window while one holds control (409, "another
  window holds control"). A viewer that names no id can watch and never take.
- **Handing back.** `POST .../display/control {"holder":"agent"}` (the [Hand back] button),
  or the holder's window staying gone for `boxdesk.GRACE_S` (10 s; reconnecting the same
  window inside it keeps control). There is no idle hand-back: an operator who stops
  typing still holds the desktop (decision 2026-10-01).
- **Three locks** (each tested alone, tests/test_desktop_control.py):
  1. `desk.act` refuses every input verb while `Desk.operator_since` is set: "the operator
     has taken control of the sandbox desktop; stop and wait" (audited, no security event).
     Screenshots stay allowed so the agent can watch.
  2. The guest seat is told `input: false` (`desk.hold_for_operator`; every later grants
     push, a Settings change included, keeps saying off while held; a seat that registers
     during a hold starts paused). The `desk_grants` row is NOT rewritten, so a host restart
     in the middle cannot leave the agent locked out, and a Settings Stop made during the
     take-over (everything off) is not undone by the hand back.
  3. The RFB filter admits key and pointer only from the holder's socket (`boxdesk.admit`);
     the clipboard is never admitted, resize and xvp never asked.
- **Hand back** also: lets go of keys and buttons the filter passed as pressed (the filter
  remembers them: `RfbInputFilter.release_bytes`), restores the seat's real grants, sets
  `Desk.frame = None` (the screen changed under the agent, so input needs a new
  screenshot) and puts one line on the agent's next desk result: "the operator used this
  desktop for N s: the screen has changed under you, ..." (first line of a normal result,
  second line of an `error:` so the loop still reads the error; summed across take-overs).
- **Audit.** `desk_actions` rows on the box's device, verb `operator_control`: `start`
  `{phase, box, by}` and `end` `{phase, box, by, why, seconds, keys, pointers}`: COUNTS of
  RFB key and pointer messages (every mouse move is one), never content, since passwords get
  typed. `approver` is the operator's name. One quiet security event for the take-over
  (`desk_operator_control`, info, `by_operator=True`, so it is filed acknowledged as "by you").
- **Shared stream.** `{"type":"display","box_id","control":{holder,viewer,by,held_s}}` on
  take and hand back; `{"type":"display","box_id","agent":{active_age_s,turns}}` (at most
  every 3 s) when the agent acts, so the window can say "agent driving" for 30 s after its
  last action without polling. The same two objects are in `GET .../display` as `control`
  and `agent`. [Stop] posts the ordinary `/api/chat/{id}/stop` for each conversation in
  `agent.turns` (those that acted in the last 2 minutes), after a confirm.
- The guest needs nothing new: Xvnc already accepts key and pointer events.
- Not covered (docs: SECURITY-RESIDUAL-RISK.md #24): the operator's keystrokes go into a box
  the agent can read; guest RFB bytes reach noVNC in the operator's origin; the pause governs
  the desk tool, not the box's shell (`xdotool` through `run_code`); an action in flight at
  the moment of take-over finishes.

## Agents can already use it

Anything run with `DISPLAY=:100` shows up in the window, for example
`DISPLAY=:100 chromium --no-sandbox ... > /dev/null 2>&1 &` through `run_code` (a
background process the command leaves behind keeps running; redirect its output).
The `screenshot` tool is separate: it uses its own Xvfb on `:99`; to look at the
live desktop use `desk(action="screenshot", computer="sandbox")`.

## Building the image

The layer adds tigervnc, openbox, xterm, tmux, xdotool, maim, and (in the
`/opt/jav3/py` venv) `httpx` and `textual`, so the jav3 terminal client can be tried
in the box without PyPI egress. Guest code needs no image rebuild (it is pushed at
boot); only the packages do:

    POST /api/vm/images/desktop/build   {"confirm": true}

Then restart the desktop box: one that booted before the deploy has no listener on
5559 and the window says so.

## Frontend

`frontend/src/panels/DesktopPanel.jsx`, its decisions in `frontend/src/desktop/logic.js`
(node-tested). `@novnc/novnc` is pinned and imported lazily (its own chunk, about
180 kB, loaded when a screen is first shown). Its top-level await needs the build
target in `vite.config.js` (es2022).

The window starts with noVNC `viewOnly` and flips it off only while this window's id is the
holder (`viewOf(ctl, viewerId, active)` in logic.js: `you` | `other` | `agent` |
`watching`). A transparent layer over the screen takes the first click or key; the
header badge, the bars and [Stop] read the same state. A window whose socket dropped while it
held control shows the bar and `reconnect`; reconnecting inside the grace keeps control.
