---
name: load_project
description: Switch the active project — loads its project.md into your context and points all file/run/todo tools at it.
when_to_use: When the operator asks you to work on a different project, or a task belongs to another project.
enabled: true
section: project
action: load
parameters:
  type: object
  properties:
    slug:
      type: string
      description: The project's slug (shown in your all-projects context, e.g. "jav3").
  required: [slug]
---
The result carries the new project.md (first 4000 chars) and your context
refreshes with it on your NEXT reply. The file tools (read_file, write_file,
edit_file, list_files) switch to the new project's files only on the next
turn: this turn they still work on the project the turn started with, or, if
it started with none, do not work at all (run_code still does, for scratch).
