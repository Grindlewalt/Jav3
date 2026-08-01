---
name: play_movie
description: Show a video file that is INSIDE A JARVIS PROJECT in a small floating player in the Jarvis browser tab. Not for the operator's own film library.
when_to_use: Only for a video that lives in the active project's files, or a direct http(s) video URL on the media allowlist — a clip you produced or were given to review. To put on one of the operator's own films use computer_play, which plays fullscreen on their actual computer. For streaming sites (YouTube, Netflix...) use computer_open_link.
enabled: true
parameters:
  type: object
  properties:
    source:
      type: string
      description: Project-relative path to a video file, or a direct http(s) URL to one.
    title:
      type: string
      description: What to show on the player (defaults to the file name).
  required: [source]
---
Same rules as play_music: the player floats in every open GUI tab; remote URLs
must be on the media allowlist (config media_hosts) or the browser blocks them.
Browsers may hold autoplay-with-sound until the operator presses play — the
controls are right there.
