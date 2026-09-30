---
name: play_music
description: Play an audio file that is INSIDE A JAV3 PROJECT in a small floating player in the Jav3 browser tab. Not for the operator's music library.
when_to_use: "Only for an audio file that lives in the active project's files, or a direct http(s) audio URL on the media allowlist — a recording you produced or were given. When the operator asks for music, use music_play instead — it searches their library."
enabled: false
section: media
action: audio
parameters:
  type: object
  properties:
    source:
      type: string
      description: Project-relative path to an audio file, or a direct http(s) URL to one.
    title:
      type: string
      description: What to show on the player (defaults to the file name).
    tab:
      type: string
      description: Which open Jav3 tab to use, by name ("the mac", "phone"). Omit it — the default is the tab the operator is talking to you from, which is almost always what they mean.
  required: [source]
---
The player floats bottom-right in ONE GUI tab (the one the operator is talking from, or `tab`), with normal controls.
Remote URLs must be on the operator's media allowlist (config media_hosts) or
the browser's CSP blocks them — the tool refuses with the allowlist so you can
tell the operator what to extend. Starting a new track replaces the current one.
