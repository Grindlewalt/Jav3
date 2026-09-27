---
name: browser_close_tab
description: Close one of Jav3's browser tabs.
when_to_use: When done with a tab.
enabled: true
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab]
---
