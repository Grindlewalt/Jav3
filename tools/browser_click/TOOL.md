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
      type: integer
      description: Element number from browser_read_page.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, element]
---
Read the page again afterwards to see what changed.
