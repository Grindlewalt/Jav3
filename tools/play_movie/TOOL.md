---
name: play_movie
description: Play a video file in a floating player on the operator's screen (mp4/webm/mkv — a project file or an allowlisted URL).
when_to_use: When the operator asks you to play a video or movie file. Pass a project-relative file path (preferred) or a direct http(s) video URL on the media allowlist. For streaming sites (YouTube, Netflix...) use open_website instead — they can't play in the floating player.
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
