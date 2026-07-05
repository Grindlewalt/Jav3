---
name: memory_write
description: Save a durable note to your memory (survives every restart and VM nuke).
when_to_use: Facts, preferences and decisions worth remembering beyond this conversation.
enabled: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Note name, e.g. "operator-preferences". Stored as memory/notes/<name>.md.
    content:
      type: string
    mode:
      type: string
      enum: [append, replace]
      description: Default append.
  required: [name, content]
---
