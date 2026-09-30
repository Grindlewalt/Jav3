---
name: memory_write
description: Save, update or delete a durable note in your memory (survives every restart and VM nuke). A note you write is PENDING until the operator approves it on the Memory page.
when_to_use: A durable fact, preference or decision about the operator, their setup or their projects. Update the note that already covers the topic; do not start a new one per event.
enabled: true
section: memory
core: true
action: write
parameters:
  type: object
  properties:
    name:
      type: string
      description: Note name, one topic per note, e.g. "operator-preferences". Stored as memory/notes/<name>.md.
    content:
      type: string
    description:
      type: string
      description: One-line summary of what the note holds. The operator reads it when reviewing the note; once approved it is the note's line in your memory index.
    mode:
      type: string
      enum: [append, replace, delete]
      description: Default append. replace rewrites the whole note (use it to correct or consolidate). delete moves it to a trash the operator can restore.
  required: [name, content]
---
A note you save is PENDING: not in your context, your index or your rules until
the operator approves it on the Memory page. Say "saved, waiting for your approval
on Memory"; never say a preference is in effect because you saved it. On a note
the operator wrote or approved, your change is a proposal they review; the note
stays as it is until then.

Keep memory small and current: a few notes, one topic each, updated in place.
When a fact changes, memory_read the note and mode=replace it with the corrected
whole; never append under a claim that is now false.

Save preferences, corrections, decisions and durable facts, each as the rule,
then **Why:**, then **How to apply:**. Convert relative dates to absolute. NOT
things derivable from files, git or a search, and NOT how Jav3's own tools or
sandbox behave (a misbehaving tool is report_harness_fault; a note about it goes
stale the day it is fixed).
