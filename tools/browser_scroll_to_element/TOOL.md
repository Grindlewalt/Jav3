---
name: browser_scroll_to_element
description: Scroll a Jav3 browser tab so a given element (by id from browser_read_page) is in view.
when_to_use: When an element is off-screen (its line said off-screen) and you want it visible before a click, type or screenshot.
enabled: true
section: browser
action: scroll_to
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    element:
      type: string
      description: Element id from browser_read_page, e.g. "f0:12".
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, element]
---
Needs a browser_read_page of this tab from this turn (element ids come from it). Scrolling alone does not need a fresh read; this does, because it targets an element.
