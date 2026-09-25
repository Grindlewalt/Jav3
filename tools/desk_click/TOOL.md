---
name: desk_click
description: Click at a point on the connected computer's screen; returns the screen afterwards.
when_to_use: To press a button, focus a field or select something you can see in the latest desk_screenshot.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    x:
      type: integer
      description: Pixels from the left of the latest screenshot.
    y:
      type: integer
      description: Pixels from the top of the latest screenshot.
    button:
      type: string
      enum: [left, right, middle]
    count:
      type: integer
      description: 2 for a double click.
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [x, y]
---
Needs a desk_screenshot of that computer from this turn (under 60 s old).
