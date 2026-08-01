---
name: todo_update
description: Add, check off, or remove items on the active project's todo list.
when_to_use: Track work items the operator should see on the board.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [add, check, uncheck, delete, list]
    text:
      type: string
      description: The item text. For add, the new item. For check/uncheck/delete, the item to act on — matched against the list, so a few distinctive words are enough.
    index:
      type: integer
      description: 0-based item index, only when `text` would be ambiguous. Positions shift as items are added, including by subagents running in parallel, so prefer text.
  required: [action]
---
Check items off by `text`, not by an index you remember. Indexes move: your own
adds and any parallel subagent's shift every position after them, so a number
read a few calls ago points at a different item — and checking off the wrong one
is worse than an error. Every call returns the current list.
