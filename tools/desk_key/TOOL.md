---
name: desk_key
description: Press a key or shortcut on the connected computer (e.g. Return, ctrl+l, super+shift+Tab); returns the screen afterwards.
when_to_use: To submit, navigate or use a keyboard shortcut.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    combo:
      type: string
      description: Modifiers and one key joined by '+'.
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [combo]
---
Session-ending combos (log out, compositor exit) are refused by the computer.
