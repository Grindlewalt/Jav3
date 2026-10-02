---
name: edit_file
description: Replace an exact text snippet in a project file. Takes effect immediately.
when_to_use: Targeted changes to an existing file.
enabled: true
section: files
core: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: File path (project-relative).
    find:
      type: string
      description: Exact text to find (must appear in the file).
    replace:
      type: string
      description: New text (the argument is `replace`); an empty string deletes `find`.
    all:
      type: boolean
      description: Replace every occurrence (default false = find must be unique).
  required: [path, find, replace]
---
`find` must match the file EXACTLY, whitespace included: read it first (read_file,
or `cat`/`sed -n` of that path in run_code; an unread edit is blocked). It must be
unique unless all:true. Live at once (git is the undo).
