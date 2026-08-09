---
name: projector_universe
description: Drive the universe simulation on the projector — pause or resume it, skip the opening, aim a surface at a different zoom level, or point it at something interesting.
when_to_use: When the operator asks about or wants to change what the space simulation is doing on the wall.
enabled: true
requires_settings: [mcp_projector_url, mcp_projector_token]
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [pause, resume, skip_opening, focus]
      description: "pause/resume the simulation; skip_opening jumps past the ten-minute cold open to the steady state; focus points the camera at a named object."
    target:
      type: string
      description: For action=focus, the object to aim at.
    surface:
      type: string
      description: For lens changes, which surface, by name or id.
    level:
      type: string
      enum: [universe, galaxy, system, planet]
      description: Zoom level for that surface.
    zoom:
      type: number
      description: Extra magnification within the level, 0.1 to 20.
    follow:
      type: string
      description: "\"drama\" follows whatever is most interesting, \"none\" holds still, \"chain:<surfaceId>\" follows another surface."
  required: []
---
Surfaces on the same level tile seamlessly into one continuous picture, so
setting two neighbouring panels to the same `level` is how you get one wide
view rather than two unrelated ones.

Restarting or regenerating the universe is deliberately NOT available — that
throws away the whole history of the simulation, which the operator may have
been running for days. If they ask for it, tell them it is theirs to do in the
app.
