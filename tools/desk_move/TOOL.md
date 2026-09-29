---
name: desk_move
description: Move the pointer on the connected computer (hover), onto an element id or a point; returns the screen afterwards.
when_to_use: To reveal a tooltip or hover menu seen in the latest desk_screenshot.
enabled: true
section: desk
action: move
requires_desk: true
parameters:
  type: object
  properties:
    element:
      type: integer
      description: Id from the element list of the latest screenshot. Moves to its centre.
    x:
      type: integer
    y:
      type: integer
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Give element, or x and y (pixels of the latest desk_screenshot), not both.
