---
name: music_download
description: Rip a track from YouTube into the operator's self-hosted library (TARMAC) with yt-dlp. Returns a job to poll.
when_to_use: When they ask you to add, save, download or "get" a song into their library from a YouTube or YouTube Music link.
enabled: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: Full http(s) YouTube or YouTube Music URL.
    job:
      type: string
      description: Instead of a url — check on a job this tool started earlier.
  required: []
---
Ripping takes a while, so this returns a job id. Call it again with `job` to see
whether it finished rather than assuming it did. Once it lands, music_search
will find it.
