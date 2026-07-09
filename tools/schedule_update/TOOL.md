---
name: schedule_update
description: Propose a recurring schedule (a task run daily at a time, or every N minutes, headlessly — as yourself or as a named agent), list existing schedules, pause one, or retract a proposal that is still awaiting approval. New schedules start PAUSED until the operator approves them.
when_to_use: The operator asks for something recurring ("read the news every morning", "check X every hour"). If the job needs a specialized role, create_agent first, then propose the schedule — and tell the operator it is waiting on their approval.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [create, list, disable, delete]
    name:
      type: string
      description: Display name for the schedule (create).
    task:
      type: string
      description: What each run should do, in plain language. Written for a fresh context that has NOT seen this conversation — include everything needed (create).
    kind:
      type: string
      enum: [jarvis, agent]
      description: Run yourself headlessly (jarvis, the default) or a named agent (create).
    agent_slug:
      type: string
      description: Which agent runs the task, when kind=agent (create).
    cadence:
      type: string
      enum: [daily, interval]
      description: Daily at a fixed time (default) or every N minutes (create).
    daily_at:
      type: string
      description: HH:MM, 24h local time, for cadence=daily. Default 09:00 (create).
    interval_minutes:
      type: integer
      description: Minutes between runs, minimum 15, for cadence=interval (create).
    project_slug:
      type: string
      description: Project context the run loads. Defaults to the active project (create).
    id:
      type: integer
      description: Schedule id (disable/delete).
  required: [action]
---
There is deliberately no enable action. A schedule is standing autonomous
execution (headless, peak pricing auto-confirmed), so only the operator can
switch one on — from the bell or the Schedules tab. After creating one, say
so plainly: "created paused, waiting for your approval." delete only retracts
proposals the operator hasn't decided on yet; ask them to remove anything
already approved.
