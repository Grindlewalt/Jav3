---
name: local_list_files
description: List files and directories on the operator's computer (a /local chat).
when_to_use: Finding your way around the local working directory.
enabled: true
requires_local: true
read_only: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Directory, relative to the local working directory (default the directory itself).
    depth:
      type: integer
      description: How many levels to descend, 1..6 (default 2).
---
Skips .git, node_modules, virtualenvs and other build clutter. Capped.
