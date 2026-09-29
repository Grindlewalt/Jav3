---
name: desk_scroll
description: Scroll on the connected computer; returns the screen afterwards.
when_to_use: To bring more of a page or list into view.
enabled: true
section: desk
action: scroll
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
    element:
      type: integer
      description: Optional element id (latest screenshot) to scroll over, e.g. a list.
    x:
      type: integer
      description: Optional point to scroll at (screenshot pixels).
    y:
      type: integer
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Pass element, or x and y together, or neither (scrolls where the pointer is).
