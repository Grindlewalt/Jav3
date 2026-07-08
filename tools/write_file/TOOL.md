---
name: write_file
description: Create or overwrite a file in the active project. The write is STAGED — the real file changes only after the operator approves it in the dashboard.
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
