---
name: music_play
description: Play music — searches the operator's library, finds the best match, and plays it. One call.
when_to_use: Whenever they ask for music by name. Just pass what they said in `query`; do not search first.
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
    where:
      type: string
      enum: [auto, jarvis, app]
      description: Which player. Leave it alone — auto uses the player inside Jarvis when a tab is open, which is the one that reliably makes sound. Pass app only if they ask for it on their phone or the music app.
    device:
      type: string
      description: An audio output, if they named one — matched against the outputs that player can actually see. Works for the Jarvis player; the music app has no output control.
    volume:
      type: integer
      description: Start level 0-100. Same destinations as device.
    tab:
      type: string
      description: Which open Jarvis tab to play in, by name ("the mac", "phone"). Omit it — the default is the tab the operator is talking to you from, which is almost always what they mean.
    queue:
      type: boolean
      description: true = add BEHIND whatever is playing instead of replacing it — "queue up", "play next", "add to the queue". Jarvis player only. On an idle player it just plays.
  required: []
---
Do not call music_search first. This searches everywhere itself and plays the
winner, so the normal case is a single call.

If it cannot tell which track was meant it returns a shortlist — play one by
passing its id. If nothing matched at all it returns the whole library, so the
next call can be the right one. Two calls is the worst case, not a conversation.

To queue SEVERAL tracks ("queue up some drive music"): one music_search by tag,
then one call here with their ids and queue=true.

There are two places sound can come out, and `auto` picks for you:

- **the Jarvis player** — a player inside the Jarvis tab. Preferred whenever a
  tab is open, because the operator is already touching that tab, and a browser
  only starts audio in a tab that has been touched. Volume and output work here.
  It plays in ONE tab: the one the operator asked from. If they say "put it on
  the mac" and they are talking to you from somewhere else, pass `tab`; the
  error lists the open tabs by name if the guess misses.
- **the music app** — TARMAC's own players on a phone or desktop. This is the one
  that goes silent: it accepts the request and plays nothing until the operator
  presses play once in that app.

It checks the sound actually started rather than trusting the acceptance, and
tells you which player it used. Do not claim music is playing when the result
says it did not start — say what the result says and, if it was the music app,
offer to move it to the Jarvis player instead.
