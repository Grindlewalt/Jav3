---
name: play_movie
description: Show a video file that is INSIDE A JAV3 PROJECT in a small floating player in the Jav3 browser tab. Not for the operator's own film library.
when_to_use: Only for a video that lives in the active project's files, or a direct http(s) video URL on the media allowlist — a clip you produced or were given to review.
enabled: true
section: media
action: video
parameters:
  type: object
  properties:
    source:
      type: string
      description: Project-relative path to a video file, or a direct http(s) URL to one.
    title:
      type: string
      description: What to show on the player (defaults to the file name).
    tab:
      type: string
      description: Which open Jav3 tab to use, by name ("the mac", "phone"). Omit it — the default is the tab the operator is talking to you from, which is almost always what they mean.
  required: [source]
---
Same rules as play_music: the player floats in one GUI tab (the one the operator is talking from, or `tab`); remote URLs
must be on the media allowlist (config media_hosts) or the browser blocks them.
Browsers may hold autoplay-with-sound until the operator presses play — the
controls are right there.
