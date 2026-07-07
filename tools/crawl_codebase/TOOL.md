---
name: crawl_codebase
description: Index the project's codebase into searchable notes under notes/codebase/ (deterministic, no LLM). Writes INDEX.md plus one detail note per top-level directory.
when_to_use: After a repo has been uploaded into the project (usually under code/), or when notes/codebase/ is missing or stale. Run it once, then navigate with search_codebase + read_file.
enabled: true
parameters:
  type: object
  properties:
    subdir:
      type: string
      description: Project subdirectory to index (default "code").
---
