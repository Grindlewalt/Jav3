---
name: deploy_agents
description: Deploy a coordinated agent team (head → task leaders → workers) on a brief. The team decomposes the work, runs workers in parallel, and returns a synthesized rollup; the live tree shows in chat and the Jobs view.
when_to_use: A multi-part task that splits into several independent subtasks (gathering across sources, analyzing several areas at once). Heavier than spawn_agent (one agent); for pure web research prefer the research tool.
enabled: true
section: agents
action: deploy
requires_project: true
parameters:
  type: object
  properties:
    brief:
      type: string
      description: What the team should accomplish, in plain language — include everything a colleague would need.
    title:
      type: string
      description: Short display title for the job (defaults to the brief's first words).
    max_rounds:
      type: integer
      description: The most tool rounds each worker gets. Default 30, maximum 60. A worker that runs out is named in the rollup as partial.
  required: [brief]
---
Node rollups are written under runs/<job>/ in the active project. Trust the
returned rollup — don't redo the team's work call-by-call.
Each worker gets 30 tool rounds (max_rounds, 60 at most); one that runs out is
named in the rollup as partial.
