---
name: journal_update
description: Append a dated entry to the active project's journal (project.md).
when_to_use: After meaningful progress, decisions, or discovered issues — keep the project's story current.
enabled: true
section: project
action: journal
requires_project: true
parameters:
  type: object
  properties:
    entry:
      type: string
      description: One concise line, under 300 characters. Do not start it with a date; the date is added.
  required: [entry]
---
