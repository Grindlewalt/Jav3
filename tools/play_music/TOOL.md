---
name: play_music
description: Play an audio file in a floating player on the operator's screen (mp3/ogg/flac/wav — a project file or an allowlisted URL).
when_to_use: When the operator asks you to play music or any audio. Pass a project-relative file path (preferred) or a direct http(s) audio URL on the media allowlist. For streaming sites (YouTube, Spotify...) use open_website instead — they can't play in the floating player.
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
