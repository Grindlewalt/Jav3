# Computer and browser navigation: operator guide

How the agent sees and clicks on your computer (`jav3-desk`) and in your
browser (`jav3-browser`). Nothing is on by default. The security trade-offs are
in `SECURITY-RESIDUAL-RISK.md` (#15, #18-#21); the wire-level contract is
`docs/navigation-contract.md`.

## Pairing a computer

1. In Jav3: **Settings → Add computer**, copy the line it shows.
2. On that computer: `jav3-desk login`, paste it, then `jav3-desk run` (or
   `jav3-desk install` to keep it running as a user service).
3. In **Settings → Computer use**, switch on what this computer may do:
   **screen** (screenshots), **input** (clicks, keys, typing, opening apps) and
   **shell** (off, ask, or trusted).

Two locks apply. The grants are yours, on the server. The computer also has its
own ceiling, which the server cannot raise: shell stays off until someone at
that keyboard runs `jav3-desk allow-shell`. `jav3-desk panic` stops everything
at once; `jav3-desk resume` brings it back.

## What the agent sees

A screenshot is scaled to at most 1280 px on its long edge, with a text list of
clickable things from the system's accessibility tree, one per line:

    [12] button "Save" @ 640,410 80x28

`@` is the centre, in pixels of the image. The front app's windows and the menu
bar are listed in full; each background window shows at most 8 buttons and
fields. Up to 150 lines are shown, on-screen ones first. Every click also
returns a new screenshot and says whether the screen changed.

macOS needs Accessibility and Screen Recording permission for the client. On
Wayland desktops and Windows there is no element list (the reason is printed),
and the agent clicks by coordinates.

Everything on screen, labels included, is untrusted text to the agent, and a
turn that looked at a screen loses "trusted" shell.

## Clicking: id, description, coordinates

- **By id**: `desk_click(element=12)`. The id must be in the latest screenshot
  from this turn; a stale or invented id is refused, never guessed.
- **By description**: `desk_click(target="the Save button")`. A label in the
  list is used if exactly one matches. Otherwise the grounding model looks at
  the screenshot and returns a point; the result says which model and how
  confident it was. The confidence is shown, not enforced. If no model is set
  up, the click is refused.
- **By coordinates**: pixels of the latest screenshot.

Input needs a screenshot from the same turn, under 60 seconds old.

**Screenshots leave your machine for description clicks.** The whole image, not
redacted, goes to the provider of the grounding model. Use a model you are
willing to show your screen to, or leave grounding unset and rely on ids.

## Zoom

`desk_screenshot(region={x,y,w,h})` (pixels of the latest full screenshot)
returns that part enlarged, up to 3x its native pixels, with its own ids and
coordinates. Use it for small icons and dense toolbars.

## Finding a grounding model: Settings → Grounding

**Find grounding model** asks every enabled image-capable model (turn models on
under API providers) to point at targets on 24 labelled screens that the server
draws itself; your own screens are never sent for testing. The table columns:

- **Hits**: how many targets it landed on, out of how many were asked.
- **Median px**: the typical miss distance in pixels (0 = inside the target).
- **p95 ms**: the slowest 5% of answers took at least this long.
- **$/1k**: estimated cost of 1000 asks, from the provider's prices.
- **Coords**: how the model writes coordinates (pixels, 0-1000 or 0-1); the
  finder picks whichever scored best.
- **unusable**: under 50% hits; never chosen automatically.

Ranking is hit rate first, then price, then speed. **Automatic** uses the top
row; the selector pins a specific model instead (or set `JARVIS_GROUNDING_MODEL`).

## The browser extension

Install and pairing steps are in `clients/jav3-browser/README.md`. Then grant it
per project under **Settings → Browser use**: **Read**, and **Act** to click and
type. It works only in its own window, asks you before it first uses each site,
and shows a notification with **Cancel** for each action. The agent can click by
element id, or by x, y on a `browser_screenshot_tab` image (also inside an
iframe); it never types into password fields.

A click is real mouse input (extension 0.6.0+), so a sign-in button that opens
a popup, like "Sign in with Google", works. To send it the extension attaches
Chrome's debugger to that one tab for the click and detaches right after:
Chrome shows **"Jav3 Browser started debugging this browser"** above Jav3's tab
for about half a second each time, and the result of every click reports how
long. Press **Cancel** on that bar and the action stops and Jav3 pauses.
Untick **Click with real mouse input** in the extension's Options to avoid the
bar; clicks are then script events, which many third-party sign-in buttons
ignore. Typing, hover and keys are still script events.

**After updating Jav3, reload the extension.** It does not update itself. Open
`chrome://extensions` (Brave: `brave://extensions`), turn on Developer mode,
press **Reload** on Jav3. Settings shows each browser's extension version and
warns when it is older than the build your server ships; an action that needs
a newer build fails with the same instructions. A build older than 0.6.0 still
clicks, with script events: the agent is told to ask you to reload it when such
a click changed nothing, and a click by coordinates into an iframe is refused
with the same instruction.

## When the screen is locked

A locked screen or a sleeping display is not worked around. The agent gets "the
screen is locked — ask the operator to unlock it" (or "the display is asleep")
and should stop and tell you. Settings shows the computer as locked or display
asleep. Unlock or wake it, and the next action works. The client detects a lock
on macOS, and on Linux when the login manager reports one.
