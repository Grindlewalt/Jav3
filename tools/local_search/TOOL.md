---
name: local_search
description: Search file contents on the operator's computer (a /local chat); returns path:line matches.
when_to_use: Finding where something is defined or used in the local working directory.
enabled: true
requires_local: true
read_only: true
parameters:
  type: object
  properties:
    query:
      type: string
      description: Text to find (case-insensitive), or a regular expression when regex is true.
    path:
      type: string
      description: File or directory to search, relative to the local working directory (default all of it).
    regex:
      type: boolean
      description: Treat query as a Python regular expression (default false).
  required: [query]
---
Skips binary files, .git, node_modules and virtualenvs. Capped at 200 matches.
