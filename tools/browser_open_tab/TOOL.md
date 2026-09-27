---
name: browser_open_tab
description: Open a URL in a new tab of Jav3's own window in the operator's browser (never the operator's tabs).
when_to_use: To look at or use a site as the operator is logged in to it, when web_read cannot (logins, JS apps). Returns the tab number.
enabled: true
requires_browser: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: http(s) URL.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [url]
---
The first action on a new site waits for the operator to allow it in the extension.
