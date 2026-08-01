---
name: computer_library
description: Browse the folders the operator granted you on their computer — subfolders with how much is in each, and the playable files at this level. One level at a time.
when_to_use: When the operator asks what films, shows or music they have, or when you need to find something and a search term is not enough ("put something on", "what's in the sci-fi folder"). Start with no arguments to see the top level, then pass a folder name to open it.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    folder:
      type: string
      description: A subfolder to open, relative to a granted folder or an absolute path inside one. Omit to list the granted folders themselves.
    kind:
      type: string
      enum: [both, audio, video]
      description: Filter to just music or just video. Defaults to both.
    limit:
      type: integer
      description: Most files to list at this level (default 60). Subfolder counts are always shown.
    client:
      type: string
      description: Which computer, by name (e.g. "macbook"). Only needed when more than one is connected; the error lists them if you guess wrong.
  required: []
---
Read-only, and it only ever sees inside the granted folders — there is no way to
list anything else on the operator's disk.

Go one level at a time rather than asking for everything: a film library is
thousands of files and listing it whole wastes the context you need for the
actual task. Subfolder counts tell you where to look next.

To play something, pass a path from here to computer_play. You do not need to
call computer_status first.
