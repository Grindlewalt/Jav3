---
name: edit_file
description: Replace an exact text snippet in a project file. Takes effect immediately.
when_to_use: Targeted changes to an existing file. `find` must match the current file text exactly.
enabled: true
section: files
core: true
requires_project: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Project-relative path of the file to edit.
    find:
      type: string
      description: Exact text to find (must appear in the file).
    replace:
      type: string
      description: The text that takes its place, named `replace` (not replacement or new_text). An empty string deletes `find`.
    all:
      type: boolean
      description: Replace every occurrence (default false = find must be unique).
  required: [path, find, replace]
---
`find` must match the current file text EXACTLY, whitespace included — so read
the file first with read_file, or print it in run_code with `cat`, `sed -n`, `head`
or `tail` of that exact path (the loop blocks an edit to a file you haven't read).
`find` must be unique in the file unless you pass all:true. The edit is live
immediately (git is the undo).
