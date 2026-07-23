---
name: git_status
description: Show the project's git status — branch, last commit, and changed/untracked files. Reflects the project's live files (writes apply immediately).
when_to_use: Before requesting a commit, or to see what canonical files have changed since the last commit.
enabled: true
read_only: true
requires_project: true
parameters:
  type: object
  properties: {}
---
