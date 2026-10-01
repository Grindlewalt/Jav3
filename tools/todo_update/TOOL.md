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
      description: The item text. For add, the new item (or pass `items` for several). For check/uncheck/delete, the item to act on — matched against the list, so a few distinctive words are enough.
    items:
      type: array
      items: {type: string}
      description: For add, several new items in one call (use this to write a plan instead of one call per item).
    index:
      type: integer
      description: 0-based item index, only when `text` would be ambiguous. Positions shift as items are added, including by subagents running in parallel, so prefer text.
  required: [action]
---
Check items off by `text`, not by an index you remember. Indexes move: your own
adds and any parallel subagent's shift every position after them, so a number
read a few calls ago points at a different item — and checking off the wrong one
is worse than an error. Add a whole plan in one call with `items`. A call
answers with the lines it changed and a count, not the whole list; use
`action: list` to see everything. The list is kept in a hidden file of its own:
it never edits or depends on a `todo.md` in the project (an existing one only
seeds the list the first time).
