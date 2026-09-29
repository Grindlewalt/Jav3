---
name: browser_back
description: Go back (or forward, with forward=true) in a Jav3 browser tab's history.
when_to_use: To return to the previous page after following a link, instead of re-navigating by URL.
enabled: true
section: browser
action: back
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    forward:
      type: boolean
      description: Go forward instead of back (default false).
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab]
---
Waits for the page to load. Element ids from earlier reads are gone afterwards; read the page again. A different site than before is asked about by the extension on the next action, like a redirect.
