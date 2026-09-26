---
name: plan_status
description: Show the project's plan run — every item's status, its agent's conversation id, and its result or error — optionally waiting until something changes. How an orchestrator supervises the team it launched with orchestrate.
when_to_use: After orchestrate has started a run, to follow it to the end. Pass wait_seconds to block until an item changes state, a message arrives for you, or the wait runs out.
enabled: false
requires_project: true
parameters:
  type: object
  properties:
    wait_seconds:
      type: integer
      description: How long to wait for a change before answering (0 = answer now; capped at 600). Use a few minutes while the run is going.
---
`enabled: false` keeps this out of ordinary chats, which are told not to wait
on a plan they launch; chat.py grants it to orchestrator conversations only.
It returns early when a message (the operator's or an agent's) is waiting for
you, so you read it on the next round instead of after the whole wait. When
the run has finished the result carries the head's closing rollup.
