---
name: computer_volume
description: Change the operator's system output volume — up, down, set to a level, mute or unmute — or move all their sound to a different speaker.
when_to_use: When the operator asks to turn it up or down, make it quieter or louder, or mute. This is the system mixer, so it affects everything playing, not just what you started. Also when they ask for the sound to come out somewhere else ("put it on the living room speakers") — that is action=output. Just call it — you do not need computer_status first.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [up, down, set, mute, unmute, output]
      description: output moves ALL of the machine's sound to the speaker named in `device`, the way the menu-bar picker does — including apps you did not start.
    percent:
      type: integer
      description: For up/down, how many points to move (default 5). For set, the level to go to (0-100).
    device:
      type: string
      description: An output id, or part of its name ("desk speakers"). Required for action=output; for the other actions it picks which speaker to adjust. Resolved against what the machine has; the error lists options on a miss. Omit for the default output.
    client:
      type: string
      description: Which machine, by name (e.g. "macbook"). Only needed when more than one is connected; the error lists them if you guess wrong.
  required: [action]
---
`device` accepts either an exact id or part of an output's name ("desk
speakers") — the client matches it against what that machine actually has and
tells you the options if it cannot. So act first; don't survey first.

Be conservative with big jumps: `set` to a high level at night is how somebody
gets startled. Prefer `up`/`down` in small steps unless the operator names a
number.

Raising the volume also clears the mute — a level set over a muted machine is
silence with a number attached. The result says when that happened, so you can
mention it rather than leaving them to wonder why it is audible again.
