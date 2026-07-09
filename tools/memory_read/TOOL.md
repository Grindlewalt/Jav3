---
name: memory_read
description: List your memory notes, or read one.
when_to_use: To recall saved notes that aren't already in your context. Your memory index (name — description) is already in context; read a note when its description looks relevant.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Note name to read. Omit to list all notes.
---
