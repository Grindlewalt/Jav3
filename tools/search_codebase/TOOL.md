---
name: search_codebase
description: Search the project's files (code + notes) for a string or regex. Returns path:line matches like grep.
when_to_use: Finding where a symbol, string or pattern lives in an uploaded codebase. Run crawl_codebase first for an overview; use read_file to open the files this finds.
enabled: true
requires_project: true
read_only: true
parameters:
  type: object
  properties:
    query:
      type: string
      description: Substring (case-insensitive) or regex to search for.
    subdir:
      type: string
      description: Limit to one project subdirectory (e.g. "code"). Default searches the whole project.
    regex:
      type: boolean
      description: Treat query as a regular expression (default false).
  required: [query]
---
