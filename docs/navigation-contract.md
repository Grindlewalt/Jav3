# Navigation: the contract for desktop and web navigation work

Branch `navigation` (off `boxes` at bff50c3). This file is the shared contract
for the four parallel work packages (WP1 desk client, WP2 desk server + tools,
WP3 grounding + model finder, WP4 browser). A change to anything here is a
contract commit by the orchestrator, announced before anyone codes against it.

## 0. Why, in one paragraph

Today the model clicks by guessing raw pixel coordinates on a 1280-px JPEG,
sees only the current frame, waits a fixed 0.6 s and has no idea whether a
click did anything. Research (OpenClaw's element registry; Anthropic/Gemini
computer-use docs; Agent S3, Skyvern, Playwright MCP; OSWorld/ScreenSpot-Pro
results) agrees on the order of operations: **structure first, pixels second,
verify every action**. So: (1) every screenshot comes with a numbered element
list from the platform accessibility tree (desk) or the DOM (browser), and the
model acts on ids; (2) when there is no element, one `locate()` function
behind the gateway grounds a description to a point, using whichever vision
model the **model finder** measured best on our own labelled screens; (3) input
verbs wait until the screen is stable and report whether it changed; the loop
keeps the last three frames so before/after is visible. No grid, no drawn
set-of-marks overlays (Anthropic measured no consistent gain); the element list
is text.

## A. Desk wire protocol (jav3-desk <-> backend/desk.py)

`PROTOCOL_V` stays 1. Everything below is additive and optional; an old client
that omits the new fields keeps working (server treats `elements` as `[]`,
`changed` as unknown).

### A.1 `screenshot` request

```
S->C req {id, verb:"screenshot", params:{
    monitor?: str,                 # name or index, as today
    region?: {x,y,w,h},            # in pixels of the LAST FULL-FRAME image of that
                                   # monitor (the server has it); the client crops
                                   # that screen rect at native resolution and
                                   # fits it to LONG_EDGE (upscale capped at 3x)
    elements?: bool,               # default true; false skips the tree walk
    walk?: "front"|"all"           # default front: the frontmost app's windows
                                   # and the menu bar in full; every other window
                                   # at most 8 buttons/fields (no rows), none at
                                   # all when the front window covers it. "all"
                                   # walks every window in full. The tool maps
                                   # desk_screenshot(elements=true|false|"all")
                                   # to front | elements:false | all; an old
                                   # client ignores walk
}}
```

### A.2 Screenshot response (also the auto-shot every input verb returns)

```
C->S res {id, ok:true, text,
    image: {mime, w, h, b64},
    frame: {monitor: str,                     # monitor name
            index: int, count: int,           # 1 of 2
            region: {x,y,w,h} | null,         # what A.1 asked for, echoed, in
                                              # FULL-frame image px; null = full
            screen: {w, h}},                  # logical size of that monitor
    elements: [ {id:int, role:str, label:str, x:int, y:int, w:int, h:int,
                 src:"ax"|"atspi"|"uia"|"dom",
                 value?: str, focused?: bool, enabled?: bool,
                 window?: str} ],                  # "menu bar" or "<App>: <title>";
                                                   # the server groups the list by it
    elements_src: "ax"|"atspi"|"uia"|"none",
    windows?: [ {window: str, background: true,   # walk=front: per capped window,
                 shown: int, total: int,          # how many are listed of how many
                 more?: bool} ],                  # (more: total is a lower bound)
    elements_note?: str,                      # why none: "no Accessibility
                                              # permission", "AT-SPI not
                                              # installed", ...
    cursor?: {x, y},                          # image px, if known
    changed?: bool,                           # auto-shots only: pixels_changed
                                              # OR elements_changed
    pixels_changed?: bool,                    # settled frame vs pre-action
                                              # 160-px thumbnail, > 8 grey
                                              # levels on > 40 pixels
    elements_changed?: bool,                  # element list (count + labels)
                                              # differs; catches overlays
    settled_ms?: int,                         # how long until two consecutive
                                              # captures matched
    timing?: {capture_ms, settle_ms,          # this request's phases on the
              elements_ms, total_ms}          # client; the server ends input-verb
                                              # results with "took 1.6 s (settle
                                              # 1.3, elements 0.2)"
}
```

Element rules:
- Boxes are in IMAGE pixels of THIS image (crop + scale already applied),
  clipped to the image; elements entirely outside the region are dropped.
- Only interactive roles (button, link, textfield, checkbox, radio, menuitem,
  tab, slider, combobox, listitem, cell with an action, toggle, image with a
  name inside a button, static text only if it is the label of a control that
  has none). Depth cap 12, count cap 400 before the server's own cap.
- `id` is 1..N in reading order (rows of 40 image px, then x). Ids are only
  valid for the frame they came with.
- The frame SERIAL is the server's, not the client's: `desk.py` numbers every
  stored frame (`frame 17`, monotonic per desk) so an old client needs nothing
  new. See B for how ids and coordinates are tied to it.
- `label` is the accessible name, else title, else value, else help text; empty
  labels are kept only for icon-only controls (src tells the model it is real).
- The tree walk must never block a screenshot: 1.5 s budget, then return what
  you have with `elements_note: "partial"`.

### A.3 New verbs

```
wait   {mode:"stable"|"change", timeout_ms:int (<= 10000)}   -> screenshot res (+changed)
drag   {x,y, to_x,to_y, button?:"left"}                       -> screenshot res
```
`CAPABILITY`: `wait` -> `screen`, `drag` -> `input`. Both go into `ACTIONS` /
`validate` on the client and `validate` on the server with the same bounds as
`click`.

### A.4 Settle

After every input verb the client captures until two consecutive downscaled
captures match (160-px thumbnail, a pixel counts as changed past 8 grey
levels, 40 such pixels = changed; the first trial's Spotlight bar was missed
at 64 px / 6 pixels), minimum 0.3 s, maximum 3 s, and reports `changed`
(pixels OR element list) against the pre-action state and `settled_ms`.
`SETTLE_S` stays as the floor. The server renders `changed: yes (elements)`
when only the list moved.

One tree walk per action: the "before" (element sig + thumbnail) is the
previous response's when it is under 2 s old and of the same frame
(monitor + zoom); otherwise the client walks and captures before acting.
The walk after settle feeds both the response and the next action's
"before". The 1.5 s budget covers the whole walk (the app list included),
AX messages time out after 0.25 s globally, and an app that timed out is
skipped for the rest of that walk. On macOS the screenshot and the
thumbnails are captured in process (CoreGraphics + ImageIO), with
screencapture + sips as the fallback.

A zoom is a look, not a mode. An input verb (and `wait`) taken from a zoomed
frame maps its x,y through that zoomed frame, but `changed`,
`pixels_changed` and `elements_changed` are judged on the WHOLE monitor, and
the automatic screenshot is the whole monitor again (`frame.region` null, a
new frame serial, `note: the zoom ended: ...`). To look closer again the model
sends `region` (always in pixels of the last full-frame image).

Once an input verb has been sent, the client never answers it with an error
that invites a repeat: a failed post-action capture is `ok` with `note: the
action was sent but the screen could not be captured afterwards — take
desk_screenshot before repeating it` (also appended to `text`), and a typing
read-back that finds the field changed but not holding the text (compared
NFKC, case-folded, whitespace-collapsed) is `ok` with `note: typed; the field
now reads differently from what was typed (autocomplete or formatting?) —
check the screenshot before retyping`. The typing error is kept for the field
AND the screen both unchanged. `desk-apps.json` entries pass the same
terminal / script-runner exclusion as the installed-app list;
`jav3-desk status` names any dropped entry.

### A.5 Locked screen / sleeping display

`hello` carries `locked` and `asleep` (booleans), and the client sends
`C->S state {locked, asleep}` whenever either changes (polled every 5 s, and
at once when a request is refused for it). macOS reads
`CGSessionCopyCurrentDictionary()["CGSSessionScreenIsLocked"]` and
`CGDisplayIsAsleep(main)`; X11 / wlroots read logind's `LockedHint` when
`loginctl` knows the session, and never report `asleep`. While locked or
asleep, screenshot / wait / input verbs are refused on both sides with
`the screen is locked — ask the operator to unlock it` or
`the display is asleep — ask the operator to wake it`; `GET /api/desk` shows
both flags. An old client never sends them and reads as awake.

`locked: null` (in `hello` or a `state` frame, sent explicitly) means
UNKNOWN: a Linux box whose locker reports nothing (logind by session, then the
ScreenSaver D-Bus, are tried first). The server does not refuse on unknown and
`jav3-desk status` prints `screen:  unknown`; the capture itself then says what
is wrong (a raw `screencapture failed: could not create image from display`
from an older client reads as `the screen could not be captured - it is
probably locked or asleep`). An omitted `locked` is `false`, as before.

## B. Desk server (backend/desk.py) and tools (tools/desk_*)

- Per desk, keep the latest frame: image bytes, w, h, monitor, region, and the
  element registry. Replace on every screenshot res. Memory only.
- Render the screenshot text for the model as:

```
screen 1280x800 of "DP-1" (monitor 1 of 2; others: "HDMI-A-1") — frame 17   | or: zoomed region 400,300 320x200 of "DP-1", shown at 1280x800 — frame 17
cursor at 612,388
elements (click by id; @ is the centre, in pixels of this image):
  [1] button "Save" @ 640,410 80x28
  [2] textfield "Search" @ 200,60 300x24 value="foo" focused
  ...
(no elements: no Accessibility permission on this computer — click by coordinates)
changed: yes, settled in 420 ms                                  | after an input verb
```
  Cap at 150 elements, in-view first, then say "+N more (zoom in with region)".
  Elements are grouped by window under `  — <window> —`; a capped background
  window reads `  — Discord: Switch Device (background, 3 of 41 shown) —`.
- `desk_click(x?, y?, element?, target?, button, count, computer?)`: exactly
  one of `(x, y)`, `element`, `target`.
  - `element`: centre of the registry box from the latest frame of that desk.
    Unknown id -> `error: element 14 is not in the latest screenshot — take
    desk_screenshot again`.
  - `frame` (optional on click / move / scroll / drag): the serial from the
    header of the result the ids or coordinates were read from. An `element`
    id or an (x, y) from any frame but the desk's latest is refused:
    `error: element 9 was listed in frame 17, but the screen is now frame 18
    — use the ids from the latest result, or take desk_screenshot`
    (`the coordinates were listed in frame 17, ...` for x, y). When `frame`
    is omitted it means the last frame whose result had already been returned
    to the model before this round began: a result returned less than
    `ROUND_GAP_S` (0.5 s) ago cannot have been read, so the second call of a
    batch (`desk_click(element=5)` then `desk_click(element=9)`) is refused
    instead of clicking whatever is now id 9. `target` is not checked (it
    names a thing, not an id).
  - `target` (a short description, "the Save button in the dialog"): first a
    WHOLE-label match on the registry (case-insensitive, punctuation ignored,
    one leading/trailing role word such as button / link / field / tab / menu
    dropped: "Save button" = "Save", but "OK" is not "Book now" and "Delete"
    is not "Delete account"; two matches is doubt); else `grounding.locate(image, w, h, target)` on the latest
    frame. Report how it was resolved in the result's first line:
    `clicked [12] button "Save" at 640,410` /
    `clicked "Save button" at 640,410 (grounded by <model>, confidence 0.82;
    element there: [12] button "Save")`. A grounded point is checked against
    the latest registry: the smallest listed element whose box holds it is
    named as above; if its label shares no word (role words and articles
    ignored) with the description the click is REFUSED: `error: the grounding
    model pointed at [31] button "Delete account", which does not match "Save
    button" — click by element id instead`; with no listed element there it
    clicks and says `; no listed element at that point` in place of the
    element (nothing is said when the registry is empty).
    Non-finite or non-numeric coordinates are refused (`x must be a whole
    number`), as is any parameter error `validate` did not foresee.
    `grounding.NotConfigured` -> `error: no grounding model; click by element
    id or coordinates, or run "Find grounding model" in Settings`.
  - Everything still goes through `act()`: grants, ceiling, fresh frame, rate,
    taint, audit — `element`/`target` only resolve to x,y BEFORE `act()`.
- `desk_move` and `desk_scroll` accept `element` the same way.
- `desk_screenshot(computer?, monitor?, region?, elements?)`; `region` is the
  zoom. `desk_wait(mode, timeout_ms, computer?)`, `desk_drag(...)`.
- Stuck detection (host side, per desk): three consecutive input verbs with
  identical verb+params and `changed=false` -> prepend to the result:
  `note: the screen has not changed after 3 identical actions; use an element
  id, zoom with region, or the keyboard`. A second screenshot with no action
  in between is allowed (the model may want a fresh frame) but the result says
  `(same as the previous screenshot)` when the thumbnail hash matches.
- Every new tool name is already in `backend/vm/broker.py:_UNTRUSTED_TOOLS`
  (contract commit). TOOL.md `requires_desk: true` as today.

## C. Grounding and the model finder (backend/grounding.py, grounding_api.py, frontend/src/GroundingPanel.jsx)

The stub in `backend/grounding.py` (contract commit) fixes the surface:

```python
@dataclass
class Located:
    x: int; y: int                 # image pixels of the image passed in
    confidence: float              # 0..1, 0.5 when the model gave none
    model: str                     # "provider/model-id"
    convention: str                # "px" | "k1000" | "unit"
    latency_ms: int

class NotConfigured(Exception): ...

async def locate(image: bytes, width: int, height: int, description: str,
                 *, op_id: str | None = None) -> Located | None
def status() -> dict            # {model, pinned, convention, ranking, probed_at, running, job}
def candidates() -> list[dict]  # enabled provider models with vision=true, "provider/model" ids
async def start_probe(models: list[str] | None, *, by: str) -> str   # background; returns job id
def to_pixels(rx: float, ry: float, convention: str, width: int, height: int) -> tuple[int, int]
```

- Model resolution: `settings.grounding_model` if set ("provider/model");
  else the probe ranking's winner; else `NotConfigured`.
- Calls go through `backend.agent.model.model.complete(messages,
  model_name=..., base_url=..., op_id=op_id, temperature=0, max_tokens=64)`
  so budgets and the ledger see them; the image is an `image_url` data-URI
  user message exactly as `backend/agent/loop.py:_image_message` builds it.
  The prompt asks for one JSON object `{"x":..,"y":..,"confidence":..}`; parse
  leniently (first `{...}` in the text).
- Conventions: the probe tries each model's answers as `px`, `k1000` (0-1000)
  and `unit` (0-1) and keeps the convention with the best hit rate; `locate()`
  uses the stored convention for that model.
- Fixtures (`backend/grounding_fixtures.py`): deterministic Pillow renders
  (seed 7), ~24 screens: 1280x800 light and dark, a 2560x1600 "Retina" capture
  downscaled to 1280x800 the way the client does, a dense toolbar with 16-px
  glyph icons, a form, a menu, a dialog over a page with a DUPLICATE label
  ("Save" in both; the target says which), small close buttons, a list of
  similar rows. Each fixture: `{png: bytes, w, h, targets: [{description,
  box:(x,y,w,h)}]}`, 3-6 targets each. Pillow is import-guarded: without it
  the probe reports "Pillow not installed" and `locate()` still works.
- Scoring per model: hit rate (point inside box, 2-px tolerance), median pixel
  error to the box (0 inside), p95 latency, cost per 1000 locates from the
  catalogue prices (input tokens estimated at 1100 per image + prompt). Rank by
  hit rate, then cost, then latency. A model with hit rate < 0.5 is marked
  "unusable". Errors (refused images, timeouts) count as misses and are
  reported.
- Storage: `<providers state dir>/grounding.json` (same `_state_path()`
  convention as providers.py): `{ranking:[{model, hit_rate, median_px, p95_ms,
  cost_per_1k, convention, n, errors}], probed_at, pinned}`.
- API (cookie routes, `require_operator`-style like desk_api.router):
  `GET /api/grounding` -> status; `POST /api/grounding/probe {models?}` ->
  `{job}`; `PUT /api/grounding {model: "provider/id" | ""}` -> status;
  `GET /api/grounding/fixtures/{i}.png` (operator-only; lets the panel show
  what the models were tested on). Emit a security event `grounding_probe`
  on start and finish with the winner.
- Panel: `GroundingPanel.jsx` mounted in `pages/Settings.jsx` after
  `DeskPanel`. One button **Find grounding model** (the model finder), a
  results table (model, hit %, median px, p95 ms, $/1k, convention), a pin
  selector ("automatic (best measured)" + each candidate), the probe time, and
  a "no image-capable models enabled" empty state linking to Providers.
- Config (contract commit): `grounding_model: str = ""`,
  `grounding_probe_targets: int = 60` (cap per model per probe, for cost),
  `grounding_timeout_s: float = 20.0`.

## D. Browser (clients/jav3-browser, backend/browser.py, tools/browser_*)

- `readPage` pierces open shadow roots, associates labels (`aria-labelledby`
  resolved to text, `<label for>`, wrapping `<label>`, `placeholder`,
  `title`), keeps icon-only buttons (empty text but a role), orders in-view
  elements first and applies the 300 cap after that ordering, and reports
  `select` options (first 20) on `<select>` elements.
- New verbs (server `validate` + extension): `select {element, value|label}`,
  `hover {element}`, `key {combo}` (same `normalize_combo` rules as the desk;
  sent to the focused element via KeyboardEvent, with Enter/Escape/Tab also
  doing their default actions where synthetic events cannot), `back`,
  `forward`. New tools: `browser_select`, `browser_hover`, `browser_key`,
  `browser_back` (with `forward: true`). Names are already in the broker's
  taint list.
- Stale element: click/type/select/hover on an id whose `data-jav3-id` element
  is gone -> `error: element f0:12 is no longer on the page — browser_read_page
  again`.
- `wait_ms` on `read_page` becomes "DOM quiet": resolve when no mutations for
  300 ms (MutationObserver) or the timeout; `read_page` and every action res
  carry `changed: bool` (document text hash vs the previous read of that tab).
- `screenshot_tab` result text lists the in-view elements of the latest read
  (`[f0:12] button "Sign in" @ 80,30 120x36` in screenshot px, converted with
  devicePixelRatio) so the picture and the ids line up.
- NOT in this pass (write down as follow-ups in the TOOL.md or a comment, do
  not build): `chrome.debugger` trusted input, file upload, dialogs.

## E. Loop and config (contract commit)

- `screenshot_keep_recent` 1 -> 3 in `backend/config.py` AND
  `guest/backend/config.py` (parity rule). Nothing else in the loop changes in
  this pass.

## F. Ownership

| WP | owns | must not touch |
|---|---|---|
| 1 desk client | `clients/jav3-desk/jav3-desk`, `tests/test_desk_client.py` | anything under backend/, tools/ |
| 2 desk server | `backend/desk.py`, `backend/desk_api.py`, `tools/desk_*`, `tests/test_desk.py`, `frontend/src/DeskPanel.jsx` (only if needed) | the client, grounding.py internals (call the stub) |
| 3 grounding | `backend/grounding.py`, `backend/grounding_fixtures.py`, `backend/grounding_api.py`, `tests/test_grounding*.py`, `frontend/src/GroundingPanel.jsx`, `frontend/src/pages/Settings.jsx` (one import + one mount line) | desk.py, browser.py, main.py (already mounted) |
| 4 browser | `clients/jav3-browser/**`, `backend/browser.py`, `backend/browser_api.py`, `tools/browser_*`, `tests/test_browser.py` | desk, grounding |

Shared and frozen by the contract commit: `backend/config.py`,
`guest/backend/config.py`, `backend/vm/broker.py`, `backend/main.py`,
`requirements.txt`, this file.

## G. Definition of done, per WP

pyflakes clean on owned files; `pytest` on owned test files green; `npm run
build` in `frontend/` green if any .jsx changed; small tool outputs (the
600 s stream watchdog); one commit per WP with a message that says what a
user can now do.

## H. Backlog: desktop + web navigation (2026-09-29, after the live checks)

State: branch `worktree-navigation`, open as PR #5 against main. Full suite at
the 19-failure macOS baseline; extension node tests 36/36; frontend builds.
The Pi runs `overnight`, which merges this branch; the session that owns
`overnight` owns Pi deploys (give it a sha, never deploy this branch alone).
Finished work is in git history, not here.

Rules for whoever picks this up: agents are Sonnet only. Ask the operator
before anything that moves their mouse or keyboard. Agents stall on 600 s
silent streams: small tool outputs, one commit per step.

Verified live on the operator's Mac (2026-09-29): screenshot with
accessibility elements; open TextEdit, click by element id, type, zoom with
`region`, close unsaved. 0.5–1.3 s per action (was 9–10 s).

### Open

O1. **DeltaMath assignment page.** On deltamath.com `read_page` now lists 34
    interactive elements and 52 candidates (it listed none), but the site
    redirected to its sign-in page and the agent cannot sign in. The operator
    signs in inside the Jav3 browser window; then rerun a read-only check:
    open an assignment, list controls, read the first problem, submit nothing.
    This is the real test of the candidates fallback (Angular `div` buttons).
O2. **The browser loads the extension from a standalone copy**
    (`~/Downloads/jav3-browser` on the operator's Mac), which was at 0.1.0 and
    is why DeltaMath failed. After ANY extension change: copy
    `clients/jav3-browser` over that folder and ask the operator to reload it.
    Worth replacing with an installer step or a served download
    (setup-must-be-code).
O3. The model took a detour in the live trial (app switcher, Escape, Cancel,
    then cmd+n) before recovering. Not a tool fault; watch for it.
O4. `walk="all"` on a desk screenshot does no occlusion filtering (it is the
    explicit "everything" mode). Browser-side stripping of bidi and control
    characters in labels, as the desk does in `_clean_str`, is not done.
O5. The zoom-refine second pass in grounding still clamps its answer instead
    of rejecting it. Off by default.

### Parked (operator: skip for now)

K1. **Flash vs Qwen3.8-27B.** Harness ready (`scripts/nav_compare.py`,
    `scripts/nav_tasks.yaml`). Needs an OpenRouter key on the Pi. Gate:
    fixture probe ≥ 0.953 and p95 ≤ 2.5 s; then 20 tasks × 3 runs, switch only
    at +15 pts success, 3:1 wins, ≤ 2× $/success. Next candidates: Holo4-27B,
    MiMo-V2.6-Flash. Large: its own day.
K2. Deferred browser: `chrome.debugger` trusted input (sites that check
    isTrusted), file upload, dialogs, coordinate clicks inside iframes.
K3. Deferred desk: Windows UIA, wlroots element coordinates, uinput backend.
