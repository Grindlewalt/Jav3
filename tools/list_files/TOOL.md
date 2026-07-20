---
name: list_files
description: List all files in the active project.
when_to_use: To see what exists before reading, editing or running anything.
enabled: true
requires_project: true
read_only: true
parameters:
  type: object
  properties: {}
---
Paths are relative to the project root. Your writes apply immediately, so
the listing always reflects your latest edits.
