---
name: browser_navigate
description: Load a URL in one of Jav3's browser tabs.
when_to_use: To go somewhere else in a tab you opened.
enabled: true
section: browser
action: navigate
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number from browser_open_tab / browser_list_tabs.
    url:
      type: string
      description: http(s) URL.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, url]
---
Element numbers from an earlier browser_read_page of this tab stop working.
