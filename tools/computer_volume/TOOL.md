---
name: computer_volume
description: Change the operator's system output volume — up, down, set to a level, mute or unmute.
when_to_use: When the operator asks to turn it up or down, make it quieter or louder, or mute. This is the system mixer, so it affects everything playing, not just what you started. Just call it — you do not need computer_status first.
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
      description: An output id, or part of its name ("desk speakers"). Resolved against what the machine has; the error lists options on a miss. Omit for the default output.
    client:
      type: string
      description: Which computer, if more than one is connected.
  required: [action]
---
`device` accepts either an exact id or part of an output's name ("desk
speakers") — the client matches it against what that machine actually has and
tells you the options if it cannot. So act first; don't survey first.

Be conservative with big jumps: `set` to a high level at night is how somebody
gets startled. Prefer `up`/`down` in small steps unless the operator names a
number.
