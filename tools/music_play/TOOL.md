---
name: music_play
description: Play music — searches the operator's library and any granted folders on their computers, finds the best match, and plays it. One call.
when_to_use: Whenever they ask for music by name. Just pass what they said in `query`; do not search first. For a film use computer_play, and for Spotify or YouTube use computer_open_link.
enabled: true
parameters:
  type: object
  properties:
    query:
      type: string
      description: What they asked for, in their words — "kick start my heart". Matched by an algorithm, so spelling and spacing do not have to be exact.
    ids:
      type: array
      items:
        type: integer
      description: Exact library ids, if a previous call handed you a shortlist. Several become a queue.
    tag:
      type: string
      enum: [drive, fast]
      description: Their two genres, for "put on something fast".
    device:
      type: string
      description: An audio output, if they named one. Only applies to a file from a granted folder — the library plays through the music app, which has no output control.
    volume:
      type: integer
      description: Start level 0-100, same restriction as device.
    client:
      type: string
      description: Which computer to search for local files, by name.
  required: []
---
Do not call music_search first. This searches everywhere itself and plays the
winner, so the normal case is a single call.

If it cannot tell which track was meant it returns a shortlist — play one by
passing its id. If nothing matched at all it returns the whole library, so the
next call can be the right one. Two calls is the worst case, not a conversation.

It also checks the sound actually started. The music app runs in a browser, and a
browser refuses to begin audio in a tab nobody has touched — so a play can be
accepted and still be silent. When that happens you are told, and the fix is for
the operator to press play once in the app. Do not claim music is playing when
the result says it did not start.
