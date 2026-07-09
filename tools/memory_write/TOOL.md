---
name: memory_write
description: Save, update or delete a durable note in your memory (survives every restart and VM nuke).
when_to_use: Facts, preferences and decisions worth remembering beyond this conversation. Also for consolidating — merging or deleting stale notes.
enabled: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Note name, e.g. "operator-preferences". Stored as memory/notes/<name>.md.
    content:
      type: string
    description:
      type: string
      description: One-line summary — this is the note's line in your always-loaded memory index, so say when the note is relevant.
    mode:
      type: string
      enum: [append, replace, delete]
      description: Default append. delete removes the note (for consolidating duplicates).
  required: [name, content]
---
Save preferences, corrections, decisions and durable facts — NOT things
derivable from files, git history or a search. For feedback/decision notes
write the rule, then **Why:**, then **How to apply:**, so future-you can judge
edge cases. Convert relative dates ("Thursday") to absolute dates. Always pass
a description: notes are recalled from the index by their description line.
