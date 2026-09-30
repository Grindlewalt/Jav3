---
name: music_play
description: Play music — searches the operator's library, finds the best match, and plays it. One call.
when_to_use: Whenever they ask for music by name. Just pass what they said in `query`; do not search first.
enabled: true
section: media
action: play
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
    where:
      type: string
      enum: [auto, jav3, app]
      description: Which player. Leave it alone — auto uses the player inside Jav3 when a tab is open, which is the one that reliably makes sound. Pass app only if they ask for it on their phone or the music app.
    device:
      type: string
      description: An audio output, if they named one — matched against the outputs that player can actually see. Works for the Jav3 player; the music app has no output control.
    volume:
      type: integer
      description: Start level 0-100. Same destinations as device.
    tab:
      type: string
      description: Which open Jav3 tab to play in, by name ("the mac", "phone"). Omit it — the default is the tab the operator is talking to you from, which is almost always what they mean.
    queue:
      type: boolean
      description: true = add BEHIND whatever is playing instead of replacing it — "queue up", "play next", "add to the queue". Jav3 player only. On an idle player it just plays.
  required: []
---
Do not call music_search first: this searches everywhere itself and plays the
winner, so the normal case is one call. Do not claim music is playing when the
result says it did not start — say what the result says and, if it was the music
app, offer to move it to the Jav3 player.

If it cannot tell which track was meant it returns a shortlist — play one by
passing its id. If nothing matched it returns the whole library, so the next
call can be the right one. Two calls is the worst case.

To queue SEVERAL tracks ("queue up some drive music"): one music_search by tag,
then one call here with their ids and queue=true.

`auto` picks the player. The Jav3 player (inside the Jav3 tab) is preferred
whenever a tab is open: a browser only starts audio in a tab the operator has
touched. It plays in ONE tab, the one they
asked from; if they say "put it on the mac" from somewhere else, pass `tab` (the
error lists the open tabs by name). The music app (TARMAC's players on a phone
or desktop) is the one that goes silent: it accepts the request and plays
nothing until the operator presses play once in that app. The result says which
player was used and whether sound actually started.
