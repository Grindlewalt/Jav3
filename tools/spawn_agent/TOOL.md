---
name: spawn_agent
description: Summon one of the defined agents to carry out a task and return its report. The agent runs its own reasoning loop with its own tools in the current project.
when_to_use: When the operator asks you to run/summon a named agent (e.g. "have the recon agent do its job"), or when a sub-task fits an agent you can see in the agent list.
enabled: true
parameters:
  type: object
  properties:
    agent:
      type: string
      description: The agent's slug (see the agent list in your context).
    task:
      type: string
      description: What the agent should do, in plain language.
  required: [agent, task]
---
Brief the agent like a smart colleague who just walked into the room — it has
NOT seen this conversation. Never delegate understanding you already have:
don't write "based on my findings, fix the bug"; give the concrete file paths,
symbols, and exactly what to change or answer.

It works in the current project; its edits land live, the same as yours
queue, and it cannot spawn further agents. You get its final report back as the
tool result — relay the useful parts to the operator.
