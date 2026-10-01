# The live desktop of a box

A box on the `desktop` image can show its screen in the web app: a Work window
(`+` menu, `/desktop`, or `/window desktop`) that watches it live. Step P1 of
three: **watch only**. P2 lets the agent drive it with the desk tool, P3 lets the
operator click in to take over and hand back.

KVM only. The desktop is an image layer (`vm/images/desktop.recipe`); there is no
Docker image of it, and a Docker box says so.

## What runs where

    browser (noVNC, view only)
      -- WebSocket /api/vm/boxes/{id}/display/ws  (cookie, same-origin, subprotocol `binary`)
    host   backend/vm/display_api.py   pins the box, filters the client bytes, splices
      -- vsock :5559 (box.json listen.display; boxes.PORT_DISPLAY)
    guest  guest/backend/display.py    Xvnc :100 1280x800 + openbox + an xterm on tmux `desk`
      -- unix socket /run/jav3-display.sock (never TCP)

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
- **Watch-only is enforced on the server.** Every client byte goes through
  `display_api.RfbInputFilter`, which parses the RFB stream and drops KeyEvent,
  PointerEvent, ClientCutText, QEMU extended key events, SetDesktopSize and xvp;
  an unknown message type ends the session. Xvnc also has the clipboard off both
  ways and ignores resize requests.
- State goes through the shared stream (topic `vm-boxes`): `{"type":"display",
  "box_id", "viewers"|"state"}`. The window never opens its own EventSource.

## Guest protocol (port 5559)

One JSON line in, one JSON line out, then (for `rfb`) raw RFB bytes:
`{"mode":"rfb"}` starts the screen if needed, answers `{"ok":true}`, splices;
`start`, `status`, `stop` answer the status. Errors are `{"ok":false,"error":...}`.
When the display starts it writes `/run/jav3-desk-apps.json` (`display.apps()`):
`terminal` (xterm on tmux `desk`) and `browser` (chromium with
`--disable-background-networking --disable-sync --no-first-run
--proxy-server=<the box's proxy> --no-sandbox`, a throwaway profile). The terminal
is opened with the screen so there is something to watch.

## For P2 and P3

- P2: add `{"mode":"desk"}` in `display._handle`; call `display.session.hold()` /
  `release()` while the agent works so the idle timer does not stop the screen under
  it. Launch apps from `display.apps()`, with `DISPLAY=:100`.
- P3: `RfbInputFilter(allow)` takes a callback per message kind (`key`, `pointer`,
  `cut_text`); `display_api.splice(ws, box, allow)` passes it through. Give it one
  that answers for whoever holds control. `resize` and `xvp` are never allowed.
  Xvnc's own `-AcceptKeyEvents` / `-AcceptPointerEvents` are on, so nothing in the
  guest changes.

## Agents can already use it

Anything run with `DISPLAY=:100` shows up in the window, for example
`DISPLAY=:100 chromium --no-sandbox ... > /dev/null 2>&1 &` through `run_code` (a
background process the command leaves behind keeps running; redirect its output).
The `screenshot` tool is separate: it uses its own Xvfb on `:99`.

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
