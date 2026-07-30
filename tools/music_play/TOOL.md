---
name: music_play
description: Play tracks from the operator's self-hosted library (TARMAC) on whichever music player they have open — phone or desktop.
when_to_use: When they ask for music from their own library. Pass ids from music_search, or a query and this will search first. For a file on a specific computer use computer_play instead; for Spotify or YouTube use computer_open_link.
enabled: true
parameters:
  type: object
  properties:
    ids:
      type: array
      items:
        type: integer
      description: Track ids from music_search. Several become a queue, in order.
    query:
      type: string
      description: Instead of ids — searched, and played if exactly one thing matches.
    tag:
      type: string
      enum: [drive, fast]
      description: Narrow a query to one of their two genres.
  required: []
---
This plays on TARMAC's own player, so one has to be open on a device. If none
is, you get told — say so rather than claiming it started.

If a query matches several tracks this lists them instead of guessing. Playing
several ids queues them in the order given.
