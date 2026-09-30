---
name: desk_key
description: Press a key or shortcut on the connected computer (e.g. Return, ctrl+l, cmd+c, shift+Tab); returns the screen afterwards.
when_to_use: To submit, navigate or use a keyboard shortcut.
enabled: true
section: desk
action: key
requires_desk: true
parameters:
  type: object
  properties:
    combo:
      type: string
      description: Modifiers and one key joined by '+', any case, e.g. "Return", "ctrl+l", "cmd+shift+t", "alt+Left".
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [combo]
---
Modifiers: ctrl, shift, alt (also option), super (also cmd, command, win, meta).
On a Mac cmd is super and the Command key; ctrl is Control, not Command.
Keys: a letter or digit, F1-F24, or Return, Enter, Tab, Escape (esc), BackSpace,
Delete, Insert, Home, End, Page_Up, Page_Down (pgup, pgdn), Left, Right, Up, Down
(arrowleft, ...), space, minus, equal, comma, period, slash, backslash,
semicolon, apostrophe, grave, bracketleft, bracketright, Print, Menu. A Mac has
no Menu, Print or F21-F24 key; the result says so. A bad name is refused before
anything is sent. Session-ending combos (log out, compositor exit) are refused
by the computer.
