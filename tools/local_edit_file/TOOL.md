---
name: local_edit_file
description: Replace exact text in a file on the operator's computer (a /local chat). Waits for the operator's approval.
when_to_use: Changing part of an existing local file. Read it first so `find` matches exactly.
enabled: true
requires_local: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Relative to the local working directory, or absolute.
    find:
      type: string
      description: Exact text to replace, including whitespace. Must occur once unless all is true.
    replace:
      type: string
    all:
      type: boolean
      description: Replace every occurrence (default false).
  required: [path, find, replace]
---
`find` must match exactly once (or pass all: true). The operator sees the
diff and approves or denies it; a denial is an error, not a cue to retry.
