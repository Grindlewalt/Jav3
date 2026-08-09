---
name: projector_output
description: Open or close the projector output window — the borderless window that actually puts the image on the wall — and toggle the calibration overlay used for aiming it.
when_to_use: When the operator wants the projection started or stopped, or wants the alignment guides on to aim something.
enabled: true
requires_settings: [mcp_projector_url, mcp_projector_token]
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [open, close]
      description: Open or close the projector output window.
    display:
      type: integer
      description: Which display, by id from projector_status. Leave out and it picks the projector automatically (the first non-primary display), which is usually right.
    calibrate:
      type: boolean
      description: Draw each surface's outline, corner pips and name over the projected image. An aiming aid — it changes nothing about the alignment.
  required: []
---
Opening the output covers the projector display entirely and floats above
everything else on that screen. Closing it puts the machine back; the editor
window and every alignment survive both.

Turning `calibrate` on is safe and reversible — it draws on top, it does not
move anything. It is the right thing to offer when the operator says the image
is landing in the wrong place, because it shows them where each surface thinks
it is.
