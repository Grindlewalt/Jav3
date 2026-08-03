---
name: clap_tracks
description: The double-clap song list — the tracks that start instantly on 👏👏, no model in the loop. Add, remove, or just see the list, in one call.
when_to_use: When the operator wants a song added to or dropped from the clap / startup-music list, or asks what is on it. To actually play something use music_play.
enabled: true
parameters:
  type: object
  properties:
    add:
      type: array
      items:
        type: string
      description: Song titles to add, in the operator's words — each is checked against the library so a typo gets flagged now, not at seven in the morning.
    remove:
      type: array
      items:
        type: string
      description: Songs to drop. Matched forgivingly (case and spacing ignored), so what they said is enough.
  required: []
---
One call does everything: pass `add` and/or `remove` together, or neither to
just see the list. The result always ends with the list as it now stands —
read it back from there, do not call again to check.

A double clap picks ONE of these at random and plays it cold, so keep the list
to songs the operator wants at any hour. Removing the last song disables the
gesture entirely until something is added back — say so if that happens.

Adds are checked against the music library as a courtesy: an unmatched title
is still added (files in granted folders play too), but the result will say
the library could not confirm it — pass that warning on.
