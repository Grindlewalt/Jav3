---
name: memory_read
description: List your memory notes, or read one.
when_to_use: To recall saved notes that aren't already in your context.
enabled: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Note name to read. Omit to list all notes.
---
