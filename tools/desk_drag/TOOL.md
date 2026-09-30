---
name: desk_drag
description: Press at one point on the connected computer's screen, drag to another and release; returns the screen afterwards.
when_to_use: To move a slider, a window, a file onto a folder, or select text by dragging, seen in the latest desk_screenshot.
enabled: true
section: desk
action: drag
requires_desk: true
parameters:
  type: object
  properties:
    x:
      type: integer
      description: Start, pixels from the left of the latest screenshot.
    y:
      type: integer
      description: Start, pixels from the top.
    to_x:
      type: integer
      description: End, pixels from the left.
    to_y:
      type: integer
      description: End, pixels from the top.
    button:
      type: string
      enum: [left, right, middle]
    frame:
      type: integer
      description: Frame number from the header of the result the coordinates came from ("frame 17"). Optional; an action on an older frame is refused.
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [x, y, to_x, to_y]
---
Needs a desk_screenshot of that computer from this turn (under 60 s old);
both points must be inside it. Use the centres from the element list when
the start or end is an element.
