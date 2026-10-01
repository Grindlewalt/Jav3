---
name: todo_update
description: Add, check off, or remove items on the active project's todo list.
when_to_use: Track work items the operator should see on the board.
enabled: true
section: project
core: true
requires_project: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [add, check, uncheck, delete, list]
    text:
      type: string
      description: For add, the new item. For check/uncheck/delete, the item to act on (a few distinctive words are enough).
    items:
      type: array
      items: {type: string}
      description: For add, several items in one call (write a whole plan at once).
    index:
      type: integer
      description: 0-based index, only when `text` is ambiguous. Positions shift as items are added; prefer text.
  required: [action]
---
Check items off by `text`, not an index you remember: indexes move as you and
parallel subagents add items, and checking off the wrong one is worse than an
error. Add a whole plan in one call with `items`. A call answers with the lines it
changed and a count; `list` shows everything. The list lives in a hidden file of
its own; a project's `todo.md` is never edited (an existing one only seeds it).
