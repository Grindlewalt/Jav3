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
      description: For add, several items at once.
    index:
      type: integer
      description: 0-based index, only if `text` is ambiguous.
  required: [action]
---
Check items off by `text`, not a remembered index (indexes shift as you and
parallel subagents add items). Add a whole plan in one call with `items`. Replies
show the changed lines and a count; `list` shows up to 40 open items. Past 45
items, old finished ones move to `.todo-archive.md`. A project's `todo.md` is never
edited (an existing one only seeds it).
