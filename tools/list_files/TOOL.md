---
name: list_files
description: List all files in the active project, marking which have unapproved staged edits.
when_to_use: To see what exists before reading, editing or running anything.
enabled: true
parameters:
  type: object
  properties: {}
---
Paths are relative to the project root. Files marked (staged) have edits of
yours awaiting operator approval — read_file shows your edited version.
