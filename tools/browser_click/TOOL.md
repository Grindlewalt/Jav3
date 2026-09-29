---
name: browser_click
description: Click an element (by id from browser_read_page) or a point (x, y on the latest browser_screenshot_tab) in a Jav3 browser tab.
when_to_use: To follow a link or press a button you saw in the latest browser_read_page of that tab (by id, including a "candidates" id). Use x, y only when neither the elements nor the candidates list has the control — e.g. a canvas or a button drawn with no markup — after a browser_screenshot_tab of that tab.
enabled: true
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
The result ends with `changed: yes/no` (whether the page text or its controls changed once it settled); read the page again to see what changed. An id that has left the page returns "no longer on the page — browser_read_page again". Clicking into a cross-origin iframe on a different site than the tab asks the operator to allow that site too. If the click opens a popup (window.open / OAuth chooser), Jav3 adopts it and reports its tab number.

A click is a real pointer sequence (pointerdown, mousedown, focus, pointerup, mouseup, click) at the element's centre, delivered to whatever is on top there — if a banner covers the element the result says so. Clicking by x, y needs a browser_screenshot_tab of that tab from this turn, under 120 s old, and the page not scrolled or navigated since; a point inside an iframe is refused (read the page and use the frame's id). Coordinate clicks need extension 0.4.0 — if the result says the extension is outdated, tell the operator to reload it. Sites that check `isTrusted` still ignore synthetic clicks (trusted input via chrome.debugger is a follow-up).
