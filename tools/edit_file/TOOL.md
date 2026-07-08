---
name: edit_file
description: Replace an exact text snippet in a project file. STAGED — takes effect after operator approval.
when_to_use: Targeted changes to an existing file. `find` must match the current file text exactly.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
    find:
      type: string
      description: Exact text to find (must appear in the file).
    replace:
      type: string
    all:
      type: boolean
      description: Replace every occurrence (default false = find must be unique).
  required: [path, find, replace]
---
