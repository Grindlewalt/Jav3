---
name: git_status
description: Show the project's git status — branch, last commit, and changed/untracked files. Reflects only approved (canonical) files; pending staged edits in .staging are NOT part of the repo.
when_to_use: Before requesting a commit, or to see what canonical files have changed since the last commit.
enabled: true
requires_project: true
parameters:
  type: object
  properties: {}
---
