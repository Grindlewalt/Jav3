---
name: computer_volume
description: Change the operator's system output volume — up, down, set to a level, mute or unmute.
when_to_use: When the operator asks to turn it up or down, make it quieter or louder, or mute. This is the system mixer, so it affects everything playing, not just what you started.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [up, down, set, mute, unmute]
    percent:
      type: integer
      description: For up/down, how many points to move (default 5). For set, the level to go to (0-100).
    device:
      type: string
      description: Audio device id from computer_status. Omit for the default output.
    client:
      type: string
      description: Which computer, if more than one is connected.
  required: [action]
---
Be conservative with big jumps: `set` to a high level at night is how somebody
gets startled. Prefer `up`/`down` in small steps unless the operator names a
number.
