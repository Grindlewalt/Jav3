---
name: browser_click
description: Click an element (by id from browser_read_page) or a point (x, y on the latest browser_screenshot_tab) in a Jav3 browser tab.
when_to_use: To follow a link or press a button you saw in the latest browser_read_page of that tab (by id, including a "candidates" id). Use x, y only when neither the elements nor the candidates list has the control — e.g. a canvas or a button drawn with no markup — after a browser_screenshot_tab of that tab.
enabled: true
section: browser
action: click
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    element:
      type: string
      description: Element id from browser_read_page, e.g. "f0:12" (f0 is the top frame, f1+ are iframes). Give this OR x, y.
    x:
      type: integer
      description: Horizontal pixel in the latest browser_screenshot_tab of this tab (not CSS px; Jav3 converts). Needs y, not element.
    y:
      type: integer
      description: Vertical pixel in that screenshot.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab]
---
The result ends with `changed: yes/no` (the page text or controls changed once it settled); read the page again to see what. It also says how the click was sent: `input: real mouse input` (extension 0.6.0+: trusted input, so sign-in popups work, in cross-origin iframes too) or `input: script events` with the reason, which pages that need a real click ignore. If the click opened a popup or new tab, Jav3 adopts it and lists its tab number: browser_read_page that tab (`changed: no` for the clicked tab is then normal). An id that has left the page returns "no longer on the page — browser_read_page again"; an element under a banner is refused, naming it. A cross-origin iframe on another site needs the operator to allow that site too.

Clicking by x, y needs a browser_screenshot_tab of that tab from this turn, under 120 s old, and the page not scrolled or navigated since. The point may be inside an iframe. Coordinate clicks need extension 0.4.0, real input and iframes 0.6.0: if the result says the extension is outdated or lacks the debugger permission, tell the operator to reload it in chrome://extensions. Chrome shows "started debugging this browser" for under a second per click.
