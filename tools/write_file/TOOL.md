---
name: write_file
description: Create or overwrite a file in the active project. The write applies immediately; never paste secret values (use {{secret:NAME}}).
when_to_use: Creating new files or full rewrites. For small changes prefer edit_file.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: File path relative to the project root.
    content:
      type: string
  required: [path, content]
---
Full-file create/overwrite, NOT a merge — the content you pass becomes the
ENTIRE file, so include everything, not just the changed lines. The write is
live immediately (git is the undo). For a small change to a file that already
exists, edit_file is cheaper and less error-prone.
