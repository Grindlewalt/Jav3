---
name: desk_wait
description: Wait on the connected computer until the screen is still (stable) or until it changes (change); returns the screen afterwards.
when_to_use: After an action that starts something slow (a page load, an app launch, a progress bar), instead of taking screenshots in a loop.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    mode:
      type: string
      enum: [stable, change]
      description: stable = until two captures match; change = until the screen differs from now.
    timeout_ms:
      type: integer
      description: Give up after this long (100..10000, default 3000).
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
---
Needs screen access only. The result says whether the screen changed and
carries a new screenshot with new element ids.
