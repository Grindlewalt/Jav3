---
name: desk_click
description: Click on the connected computer's screen, by element id, by description or at a point; returns the screen afterwards.
when_to_use: To press a button, focus a field or select something you can see in the latest desk_screenshot. Prefer element (the [id] from its element list); use target when the thing has no id; x/y last.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    element:
      type: integer
      description: Id from the element list of the latest screenshot ([12] -> 12). Clicks its centre.
    target:
      type: string
      description: A short description when there is no id, e.g. "the Save button in the dialog".
    x:
      type: integer
      description: Pixels from the left of the latest screenshot (with y).
    y:
      type: integer
      description: Pixels from the top of the latest screenshot (with x).
    button:
      type: string
      enum: [left, right, middle]
    count:
      type: integer
      description: 2 for a double click.
    frame:
      type: integer
      description: Frame number from the header of the result the ids or coordinates came from ("frame 17"). Optional; an action on an older frame is refused.
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Give exactly one of element, target, or x and y. Needs a desk_screenshot of
that computer from this turn (under 60 s old); ids are only valid for the
latest result (every click returns a new frame with new ids; its header says
"frame N", and one round's second click on the old ids is refused). The first
line of the result says what was clicked and how it was found. A target is
first matched against the element labels, then located on the image by the
grounding model; if none is set up, click by id or coordinates.
