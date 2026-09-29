---
name: browser_list_tabs
description: List the tabs Jav3 has open in the operator's browser (only its own).
when_to_use: To find a tab number again.
enabled: true
section: browser
action: list_tabs
requires_browser: true
parameters:
  type: object
  properties:
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
---
