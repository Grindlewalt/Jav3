---
name: spawn_temp_agent
description: Spawn a disposable copy of yourself for one task — you write its role prompt, it runs once and is gone. No roster entry (use create_agent only for roles worth keeping). It reports back, and if it built something durable it leaves a memory note of what and how.
when_to_use: A one-off subtask no saved agent covers — offloading a build or investigation to a worker instead of doing it inline. Set duplicate=true ONLY when the task truly needs your full context (memory notes, user profile, all-projects, roster); the lean default is much cheaper per iteration.
enabled: true
parameters:
  type: object
  properties:
    task:
      type: string
      description: What the agent should do, in plain language — everything a colleague who has NOT seen this conversation needs (exact paths, symbols, what done looks like).
    prompt:
      type: string
      description: The agent's role for this job, second person ("You are..."), a few sentences — scope, method, constraints. It is layered on top of the shared context.
    duplicate:
      type: boolean
      description: true = full copy of your context (memory notes, user profile, all-projects, agent roster) — costlier every iteration. Default false = lean worker (environment + active project only).
    label:
      type: string
      description: Short display name for the Jobs view, e.g. "css refactor".
  required: [task, prompt]
---
Brief it like a smart colleague who just walked into the room — it has NOT
seen this conversation. Never delegate understanding you already have: give
concrete file paths, symbols, and exactly what to change or answer.

It works live in the current project under the same write scans as you. If it
builds anything it records a memory note (untrusted until the operator
promotes it, like all agent notes) and its final report returns as this
tool's result — relay the useful parts to the operator. Temp agents nest like
spawn_agent: two levels deep on one shared token budget.
