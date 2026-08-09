---
name: projector_show
description: Put something on a projected surface — a procedural space scene, the universe simulation, a video or image, a solid colour, a calibration grid, or the live voice display. Also fades and hides surfaces.
when_to_use: Any time the operator asks for something to go on the wall or ceiling. Name the surface the way they did ("the ceiling", "surface two") and the right one is found — you do not need projector_status first.
enabled: true
requires_settings: [mcp_projector_url, mcp_projector_token]
parameters:
  type: object
  properties:
    surface:
      type: string
      description: Which surface, by name or id, however the operator said it ("ceiling", "Wall", "2").
    show:
      type: string
      enum: [scene, starfield, nebula, orbital, sim, grid, color, image, video, voice]
      description: "What to put there. scene/starfield/nebula/orbital are procedural space art; sim is the live universe simulation; grid is the calibration target; voice shows what you are hearing and saying."
    path:
      type: string
      description: Absolute file path, for show=image or show=video. Only files in the folders the operator granted the projector are playable.
    color:
      type: string
      description: Hex colour like "#1a2b3c", for show=color.
    seed:
      type: integer
      description: Reseed a procedural scene to get a different one. Optional.
    visible:
      type: boolean
      description: Show or hide the surface. Can be used on its own.
    opacity:
      type: number
      description: Fade the surface, 0 to 1. Can be used on its own.
  required: [surface]
---
One call does the whole request: naming the surface, choosing the content and
setting the level are all this tool. Do not call projector_status first to look
up an id — pass the operator's own words in `surface`.

You cannot move, warp, resize or delete a surface, and you should not offer to.
The operator aligns those to a physical wall by hand and that alignment is the
expensive thing in the room; there is no tool for it on purpose.

`show=voice` puts your own state on the wall — what you heard and what you are
saying. Useful when they want to see the conversation from across the room.

If a video path is refused, it is outside the folders the operator granted the
projector app. Say so plainly; you cannot widen that yourself.
