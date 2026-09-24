---
name: music_status
description: The operator's music — library size, and what each of the two players (the one inside Jav3 and the music app) is doing right now.
when_to_use: When they ask what's playing. You rarely need it before music_play, which picks a working player by itself.
enabled: true
read_only: true
parameters:
  type: object
  properties: {}
---
Two players report here and they cannot see each other:

- **Jav3 player** — the one inside the Jav3 tab. "no tab open" means there is
  nowhere in-page to play; "idle" means a tab is open and ready.
- **music app** — TARMAC's own PWA players. `players open: 0` means nothing there
  to play on, which no longer blocks anything: the Jav3 player works without it.

If a track is loaded but no sound was confirmed, say exactly that rather than
"it's playing" — it is the difference the operator hit as silence.
