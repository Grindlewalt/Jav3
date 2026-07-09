---
name: git_diff
description: Show the uncommitted diff of the project's canonical files (optionally limited to one path). Untracked files are listed by name. Staged edits pending operator approval are not included.
when_to_use: To review exactly what would go into a commit before calling git_commit_request.
enabled: true
read_only: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Limit the diff to this file or directory (relative to the project root).
---
