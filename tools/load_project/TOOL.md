---
name: load_project
description: Switch the active project — loads its project.md into your context and points all file/run/todo tools at it.
when_to_use: When the operator asks you to work on a different project, or a task belongs to another project.
enabled: true
parameters:
  type: object
  properties:
    slug:
      type: string
      description: The project's slug (shown in your all-projects context, e.g. "jarvis-v3").
  required: [slug]
---
Your context refreshes with the new project.md on your NEXT reply — within
this turn, use read_file on project.md if you need its contents immediately.
