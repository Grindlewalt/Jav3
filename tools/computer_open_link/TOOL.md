---
name: computer_open_link
description: Open an http(s) URL in the browser on the operator's actual computer (not in the Jarvis tab).
when_to_use: When the operator wants a page up on their machine — including streaming sites like YouTube, Netflix or Spotify, which cannot be played as files. Use open_website instead if you only want it to appear in the Jarvis GUI tab. No need to check computer_status first.
enabled: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: Full http(s) URL. Nothing else is accepted.
    screen:
      type: integer
      description: Monitor index from computer_status, if the operator named one.
    client:
      type: string
      description: Which machine, by name (e.g. "macbook"). Only needed when more than one is connected; the error lists them if you guess wrong.
  required: [url]
---
Only http and https are accepted — file:, javascript: and the rest are refused
by both the server and the client, so do not try to route around it.

Screen placement depends on the window manager. If the browser is already open
it will usually land wherever that window lives, and the result will say so
rather than pretend otherwise.
