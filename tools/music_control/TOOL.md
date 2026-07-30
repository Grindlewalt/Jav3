---
name: music_control
description: Pause, resume, or skip on the operator's self-hosted music player (TARMAC).
when_to_use: When they say pause, resume, skip or go back AND the music is coming from their own library app. If it could be anything playing on a computer — Spotify, a browser tab — use computer_playback instead, which drives whatever has the system's attention.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [pause, resume, next, prev]
  required: [action]
---
TARMAC's vocabulary is pause / resume / next / prev — there is no stop, and it
is "prev" not "previous". Use these exact words.

If no player is open you get told, rather than it silently doing nothing.
