---
name: orchestrate
description: Turn a big ask — a dump of requirements, notes or a spec — into an explicit, saved checklist of work items and run a team of agents through it, one agent per item, dependencies respected, siblings coordinating by message.
when_to_use: When the operator hands over a large multi-part task (a spec, "build X with A, B and C", a long list) that is too big for one turn and has parts that can proceed in parallel. For one focused sub-task use spawn_agent; for web research use research.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    dump:
      type: string
      description: Everything the planner needs, verbatim — the operator's ask, requirements, constraints, relevant facts. The agents doing the items will NOT see this conversation, only the checklist briefs made from it.
    files:
      type: array
      items:
        type: string
      description: Project-relative paths of files the planner should read alongside the dump (a spec, a README).
    run:
      type: boolean
      description: Start the run right away (default true). false only plans, so the operator can edit the checklist first.
    models:
      type: array
      description: ONLY when the operator explicitly said which model to use for which task. One entry per assignment; the items doing that task run on that model, every other item on the default. Omit otherwise — never choose a model yourself.
      items:
        type: object
        properties:
          task:
            type: string
            description: The work the operator assigned the model to, in their words.
          model:
            type: string
            description: The model id (provider/model) the operator named.
        required: [task, model]
    title:
      type: string
      description: A short name for the plan (under ~8 words) — what it is for, not how the dump starts. Shown in the agents tree and the Jobs view.
  required: [dump]
---
The checklist is saved to the project's .plan.json and shown on the Workspace
Plan panel, where the operator can edit, reorder, skip or mark items while the
run is live. The run is detached: this call returns as soon as it has started.
Do not wait for it in this turn — tell the operator it is running and where to
watch it (the Plan panel, or the run tree). Each item is a separate agent run
under this plan's head; items report with plan_report and the head writes a
closing rollup into runs/<job>/ when the last item settles.
