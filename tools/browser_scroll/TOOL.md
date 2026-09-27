---
name: browser_scroll
description: Scroll a Jav3 browser tab by whole screens.
when_to_use: To reach content further down (or up) before reading or taking a screenshot.
enabled: true
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    pages:
      type: integer
      description: Screens to scroll, -10..10 (negative is up; default 1).
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab]
---
