---
name: play_music
description: Play an audio file that is INSIDE A JARVIS PROJECT in a small floating player in the Jarvis browser tab. Not for the operator's music library.
when_to_use: Only for an audio file that lives in the active project's files, or a direct http(s) audio URL on the media allowlist — a recording you produced or were given. When the operator asks for music, use music_play instead: it searches their library and the folders granted on their computers. For streaming sites (YouTube, Spotify...) use computer_open_link.
enabled: true
parameters:
  type: object
  properties:
    source:
      type: string
      description: Project-relative path to an audio file, or a direct http(s) URL to one.
    title:
      type: string
      description: What to show on the player (defaults to the file name).
  required: [source]
---
The player floats bottom-right in every open GUI tab, with normal controls.
Remote URLs must be on the operator's media allowlist (config media_hosts) or
the browser's CSP blocks them — the tool refuses with the allowlist so you can
tell the operator what to extend. Starting a new track replaces the current one.
