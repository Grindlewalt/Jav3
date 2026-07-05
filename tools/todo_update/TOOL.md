---
name: todo_update
description: Add, check off, or remove items on the active project's todo list.
when_to_use: Track work items the operator should see on the board.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [add, check, uncheck, delete, list]
    text:
      type: string
      description: The item text (for add).
    index:
      type: integer
      description: 0-based item index (for check/uncheck/delete).
  required: [action]
---
