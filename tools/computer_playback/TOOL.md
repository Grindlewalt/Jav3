---
name: computer_playback
description: Control whatever is already playing on the operator's computer — pause, resume, skip to the next track, go back to the previous one, or stop.
when_to_use: When the operator says pause, resume, skip, next, go back, previous, or stop. Works on any media player that reports itself to the desktop (Spotify, a browser tab, mpv, a music app) — you do not need to have started it.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [play, pause, playpause, next, previous, stop]
    client:
      type: string
      description: Which computer, if more than one is connected.
  required: [action]
---
"Back a song" is `previous`. Note that most players treat one `previous` as
"restart this track" and two as "the track before" — if the operator clearly
means the earlier song and the current one just restarted, send it again.

If nothing is playing this returns an error rather than doing nothing silently;
say so instead of claiming you paused something.
