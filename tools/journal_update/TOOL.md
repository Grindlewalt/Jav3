---
name: journal_update
description: Append a dated entry to the active project's journal (project.md).
when_to_use: After meaningful progress, decisions, or discovered issues — keep the project's story current.
enabled: true
parameters:
  type: object
  properties:
    entry:
      type: string
      description: One concise journal line.
  required: [entry]
---
