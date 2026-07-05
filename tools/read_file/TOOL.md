---
name: read_file
description: Read a file from the active project (your staged edits included, if any).
when_to_use: Before editing a file, or to check contents.
enabled: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: File path relative to the project root.
  required: [path]
---
