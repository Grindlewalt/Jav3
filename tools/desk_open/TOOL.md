---
name: desk_open
description: Open a URL in the connected computer's browser, or launch one of the apps it offers; returns the screen afterwards.
when_to_use: To start a task on that computer faster than clicking to it.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: An http(s) URL.
    app:
      type: string
      description: An app name from the list the computer offers (an error lists them).
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Give url or app, not both.
