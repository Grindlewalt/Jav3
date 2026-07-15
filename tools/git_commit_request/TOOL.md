---
name: git_commit_request
description: Request a git commit of the project's canonical files. Nothing is committed or pushed until the operator approves the request — this only files it.
when_to_use: When a coherent unit of approved work should be recorded in history. Write a clear imperative commit message; optionally limit to specific paths.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    message:
      type: string
      description: The commit message (imperative, e.g. "Add data loader").
    paths:
      type: array
      items:
        type: string
      description: Only commit these paths. Omit to commit all changes.
  required: [message]
---
Commits the CANONICAL, already-approved files — NOT your staged edits. So get
your write_file/edit_file changes approved first, then request the commit;
otherwise it records nothing new. Nothing commits or pushes until the operator
approves this request too.
