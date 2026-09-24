---
name: plan_report
description: Report the outcome of the plan item you are working on — done, failed or blocked — with a summary the items after you will read. Required before you stop.
enabled: false
parameters:
  type: object
  properties:
    status:
      type: string
      enum: [done, failed, blocked]
      description: done = the item is complete. failed = something went wrong that a retry could fix. blocked = it needs the operator (say what).
    summary:
      type: string
      description: What you did and where (exact paths), what the next items need to know; for failed/blocked, what went wrong or what is needed.
    item:
      type: string
      description: Your item id (e.g. i3), for the record. Optional — you can only ever report your own item.
  required: [status, summary]
---
`enabled: false` keeps this out of every ordinary turn: the plan runner
(backend/plan.py) grants it to the agent running a checklist item and to
nobody else. The item is resolved host-side from the turn's conversation — an
item can only report itself, whatever `item` says.

Call it once, when the item is finished or cannot be finished, then end your
reply. A final reply without it counts as a failed attempt.
