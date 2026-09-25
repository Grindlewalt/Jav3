---
name: desk_move
description: Move the pointer on the connected computer (hover); returns the screen afterwards.
when_to_use: To reveal a tooltip or hover menu seen in the latest desk_screenshot.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    x:
      type: integer
    y:
      type: integer
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [x, y]
---
Coordinates are pixels of the latest desk_screenshot.
