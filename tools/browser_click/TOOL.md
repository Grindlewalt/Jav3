---
name: browser_click
description: Click an element (by number from browser_read_page) in a Jav3 browser tab.
when_to_use: To follow a link or press a button you saw in the latest browser_read_page of that tab.
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
      description: Element id from browser_read_page, e.g. "f0:12" (f0 is the top frame, f1+ are iframes).
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, element]
---
The result ends with `changed: yes/no` (whether the page text or its controls changed once it settled); read the page again to see what changed. An id that has left the page returns "no longer on the page — browser_read_page again". Clicking into a cross-origin iframe on a different site than the tab asks the operator to allow that site too. If the click opens a popup (window.open / OAuth chooser), Jav3 adopts it and reports its tab number.
