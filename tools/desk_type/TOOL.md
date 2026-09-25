---
name: desk_type
description: Type text into whatever has focus on the connected computer; returns the screen afterwards.
when_to_use: After clicking into a field in the latest desk_screenshot.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    text:
      type: string
      description: At most 2000 characters. Use desk_key for Enter, Tab and shortcuts.
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [text]
---
Text containing a stored secret's value is refused.
