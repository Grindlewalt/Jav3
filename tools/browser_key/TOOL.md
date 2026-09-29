---
name: browser_key
description: Press a key or key combo on the focused element of a Jav3 browser tab — Enter, Escape, Tab, shift+Tab, arrows, ctrl+a, a single letter.
when_to_use: To submit with Enter, close a popup with Escape, move between fields with Tab, or drive arrow-key widgets. Click or type into a field first so it has focus. Use browser_type for text.
enabled: true
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    combo:
      type: string
      description: 'Key or combo, modifiers joined with "+": "Enter", "Escape", "Tab", "shift+Tab", "Down", "Page_Down", "ctrl+a", "F5". Modifiers: ctrl, alt, shift, super (meta / cmd).'
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, combo]
---
Needs a browser_read_page of this tab from this turn. The key events are synthetic, so when the page does not cancel the keydown Jav3 performs the default action itself: Enter submits the field's form (or activates a focused button / link), Escape closes an open dialog / details and blurs, Tab / shift+Tab moves focus to the next / previous focusable element, Space activates a focused button or checkbox. Browser shortcuts (ctrl+t, ctrl+w, F5) do nothing. The result says what happened and ends with `changed: yes/no`.
