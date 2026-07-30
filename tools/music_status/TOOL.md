---
name: music_status
description: The operator's self-hosted music server (TARMAC) — how big the library is, whether a player is open, and what is playing right now.
when_to_use: When they ask what's playing, or before music_play if you need to know whether a player is actually open to play on.
enabled: true
read_only: true
parameters:
  type: object
  properties: {}
---
`players_connected` is the one that matters: playback goes to an open TARMAC
player (the music app on a phone or desktop), so with none open there is nothing
to play on and music_play will say so.

This is a different thing from computer_play, which runs a file through the
player on a computer you have a client on.
