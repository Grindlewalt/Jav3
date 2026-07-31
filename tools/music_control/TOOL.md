---
name: music_control
description: Pause, resume, skip, go back, set the volume, or stop the operator's music.
when_to_use: When they say pause, resume, skip, go back, louder/quieter or stop AND the music is coming from their own library. If it could be anything playing on a computer — Spotify, a browser tab — use computer_playback instead, which drives whatever has the system's attention.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [pause, resume, next, prev, volume, stop]
    level:
      type: integer
      description: 0-100, required for volume.
    where:
      type: string
      enum: [auto, jarvis, app]
      description: Which player. Leave it alone — auto follows whichever one is actually holding a track.
  required: [action]
---
It is "prev", not "previous".

Two players can be playing, and they are not equally capable:

- **the Jarvis player** (inside the Jarvis tab) does all six actions.
- **the music app** (TARMAC's own PWA) does pause, resume, next and prev only —
  it has no volume and no stop.

`auto` sends the action wherever the music currently is, so normally you do not
pass `where` at all. If they ask for volume or stop while the music app is the
one playing, you are told it cannot do that — offer to move the track to the
Jarvis player rather than claiming it worked.
