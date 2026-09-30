---
name: browser_hover
description: Move the pointer over an element (by id from browser_read_page) in a Jav3 browser tab, to open a hover menu or tooltip.
when_to_use: When a menu or tooltip appears only on hover. Read the page again afterwards to get the ids of what appeared.
enabled: true
section: browser
action: hover
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
Needs a browser_read_page of this tab from this turn. Sends pointerover / mouseover / mousemove to the element (a page-script hover). Pure-CSS :hover menus do not open from synthetic events; if the result says `changed: no`, click the element instead.
