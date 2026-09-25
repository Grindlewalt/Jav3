---
name: desk_scroll
description: Scroll on the connected computer; returns the screen afterwards.
when_to_use: To bring more of a page or list into view.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    dy:
      type: integer
      description: Wheel clicks, positive = down (-20..20).
    dx:
      type: integer
      description: Wheel clicks, positive = right (-20..20).
    x:
      type: integer
      description: Optional point to scroll at (screenshot pixels).
    y:
      type: integer
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Pass x and y together or not at all.
