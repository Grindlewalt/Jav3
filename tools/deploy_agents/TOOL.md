---
name: deploy_agents
description: Deploy a coordinated agent team (head → task leaders → workers) on a brief. The team decomposes the work, runs workers in parallel, and returns a synthesized rollup; the live tree shows in chat and the Jobs view.
when_to_use: A multi-part task that splits into several independent subtasks (gathering across sources, analyzing several areas at once). Heavier than spawn_agent (one agent); for pure web research prefer the research tool.
enabled: true
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
  required: [brief]
---
Node rollups are staged under runs/<job>/ in the active project. Trust the
returned rollup — don't redo the team's work call-by-call.
