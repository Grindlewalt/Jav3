---
name: read_file
description: Read a file from the active project (your own edits this turn included).
when_to_use: Before editing a file, or to check contents.
enabled: true
requires_project: true
read_only: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: File path relative to the project root.
    offset:
      type: integer
      description: 1-based line number to start reading from. Omit to read the whole file.
    limit:
      type: integer
      description: Number of lines to return, starting at offset. Use with offset to read a big file in slices.
  required: [path]
---
