# jav3-browser

A Chromium extension (Chrome, Brave, Edge) that lets your Jav3 server use this
browser — in its own window, on sites you allow, while you can see and stop it.

## Install

1. Get the folder: download `http://<your-jav3>/cli/jav3-browser.zip` and unzip
   it (or use `clients/jav3-browser/` from a checkout).
2. Open `chrome://extensions` (Brave: `brave://extensions`, Edge:
   `edge://extensions`).
3. Turn on **Developer mode** (top right).
4. **Load unpacked** → pick the `jav3-browser` folder.
5. Pin the Jav3 icon to the toolbar (puzzle-piece menu → pin).

## Pair

1. In Jav3: **Settings → Add computer** → copy the `address=… code=…` line
   (valid for 10 minutes, single use).
2. In the extension: right-click the icon → **Options** → paste the line,
   optionally name the browser, **Pair**.
3. In Jav3: **Settings → Browser use** → grant the browser to a project
   (**Read**, and **Act** if it may click and type). Nothing is granted by
   default.

The popup shows the connection, the big **Pause** button, sites waiting for
your answer, and **Disconnect** (revokes the token).

## What it can do

A closed list of verbs, checked on the server and again in the extension
(`lib/verbs.js`): `open_tab`, `navigate`, `read_page` (text plus a numbered
list of links/buttons/fields), `click`, `type`, `scroll`, `screenshot_tab`,
`close_tab`, `list_tabs`.

- **Only its own tabs.** Jav3 opens tabs in a separate, unfocused window
  (grouped as "Jav3") and refuses any tab it did not open. Your tabs, focus and
  windows are never touched; screenshots capture only Jav3's tab.
- **Per-site consent.** The first action on a new site asks you (notification
  and popup: Allow / Deny, 60 s, silence = no). Pre-allow or block sites in
  Options (`example.com`, `*.example.com`, `!blocked.example`).
- **You see every action.** A notification ("Jav3 is clicking on
  example.com") with **Cancel**, which aborts the action and pauses Jav3's
  access until you resume. The notifications can be turned off in Options; the
  site questions cannot.
- **Never:** the Jav3 server's own pages, non-http(s) URLs, URLs with
  `user:pass@`, password or file fields, typed text or URLs containing a
  stored secret's value (refused by the server).

## How it connects

The service worker dials OUT over a WebSocket to `/api/browser/ws` and sends
its `browser`-scoped device token in the first frame. That token opens nothing
else: chat, the desk socket and every Settings route refuse it. See
`backend/browser.py` and SECURITY-RESIDUAL-RISK.md #18.

## Test

    node --test clients/jav3-browser/test/verbs.test.mjs
