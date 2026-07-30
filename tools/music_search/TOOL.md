---
name: music_search
description: Search the operator's self-hosted music library (TARMAC) by title, artist or album. Returns track ids you can pass to music_play.
when_to_use: When they name music to put on and you need its id, or when they ask what's in the library. Use the `tag` filter if they ask for one of their two genres — "drive" or "fast".
enabled: true
read_only: true
parameters:
  type: object
  properties:
    query:
      type: string
      description: Title, artist or album text. Substring match.
    tag:
      type: string
      enum: [drive, fast]
      description: Only their "drive" or "fast" tracks. The library has exactly these two genres.
    limit:
      type: integer
      description: Most results to return (default 25, max 100).
  required: [query]
---
An empty query with a `tag` lists everything carrying that tag, which is how to
answer "put on something fast".

Pass the ids to music_play. Don't invent ids — they only come from here.
