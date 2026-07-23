---
name: open_website
description: Open a URL in a new browser tab on the operator's screen (every open Jarvis GUI tab receives it). Purely visual — nothing is fetched server-side.
when_to_use: When the operator asks to "pull up", "open" or "show" a website, dashboard, video page or doc — or when the best answer is the live page itself rather than a summary of it.
enabled: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: Full http(s) URL to open.
  required: [url]
---
Only http/https URLs. If the browser's popup blocker stops the tab, the GUI
shows a clickable toast instead — say so if the operator reports nothing
happened. If no GUI tab is connected the call tells you; give the operator the
URL in text instead.
