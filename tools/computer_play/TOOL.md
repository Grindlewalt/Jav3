---
name: computer_play
description: Play music or a movie on the operator's computer — from a folder they have granted you, or from their Jellyfin library. Can target a specific monitor and a specific audio output.
when_to_use: When the operator asks you to put something on. Name the track, album or film in `query` and the right file gets found; pass `path` only if you already have an exact one. Call it directly — if they named a speaker or screen, just pass the name; you do not need computer_status first.
enabled: true
parameters:
  type: object
  properties:
    query:
      type: string
      description: What to play — part of a filename, track, album or title. Matched against the granted folders, then Jellyfin.
    path:
      type: string
      description: An exact file path, if you already have one from a previous search.
    kind:
      type: string
      enum: [audio, video]
      description: audio for music, video for a movie or show. Defaults to audio.
    source:
      type: string
      enum: [auto, local, jellyfin]
      description: Where to look. auto tries the granted folders first, then Jellyfin.
    screen:
      type: integer
      description: Monitor index (0-based), for video, if the operator named a screen.
    device:
      type: string
      description: An output id, or part of its name, if the operator named one. Resolved against the machine's own list.
    volume:
      type: integer
      description: Start volume 0-100 for this playback only. Leave out to keep the current level.
  required: []
---
`device` and `screen` take an exact value or part of a name; if it does not
match, the error lists what that machine has. Use computer_library to see what
is there rather than guessing at filenames.

Only the folders the operator granted on the Computer use tab are reachable,
and only real audio/video files in them — you cannot browse their disk, and a
path outside a granted folder is refused by the server and again by the client.

Be careful about volume at night. If the operator has not asked for a level,
don't set one; starting something loud is worse than starting it quiet.

If several files match, this returns the candidates instead of guessing — show
them and ask which. For streaming services (Spotify, YouTube, Netflix) there is
no file to play: use computer_open_link.
