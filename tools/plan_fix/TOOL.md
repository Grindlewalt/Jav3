---
name: plan_fix
description: Repair the project's plan run instead of handing its failures to the operator — re-dispatch a failed or blocked item with guidance, accept an item whose work you verified yourself, edit an item's brief or dependencies, add an item, or skip one. Relaunches the run if it had stopped.
when_to_use: As soon as plan_status shows an item failed or blocked. Work out the cause first (its error, how far it got, the files it wrote). If the work is in place and you have checked it (run the proof, read the files), accept it. Otherwise retry with guidance that removes the cause, or edit/add/skip to change the plan itself.
enabled: false
internal: true
section: plans
action: fix
requires_project: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [retry, edit, add, skip, accept]
      description: retry = back to todo with your guidance (required); accept = mark the item done on your own verification (summary required), no agent runs; edit = new title/brief/depends_on and/or guidance; add = a new item (title + brief, optional depends_on); skip = settle an item that is moot.
    item:
      type: string
      description: The item id (e.g. i3). Not used by add.
    guidance:
      type: string
      description: What the next attempt must do differently — the concrete fix, the file to create, the assumption to make, the approach to switch to. The item's agent reads it at the top of its task.
    summary:
      type: string
      description: For accept, what you verified (the command that passed, the files that exist). It becomes the item's result.
    title:
      type: string
      description: For add or edit.
    brief:
      type: string
      description: For add or edit — self-contained instructions, exact paths, the command that proves it works.
    depends_on:
      type: array
      items:
        type: string
      description: For add or edit — ids of items whose results this one needs.
    run:
      type: boolean
      description: Relaunch the run if it is not running (default true).
  required: [action]
---
`enabled: false` keeps this out of ordinary turns; chat.py grants it to
orchestrator conversations only, beside plan_status. A retry keeps the item's
earlier attempts (their errors and how far they got) in its next task, so the
agent continues rather than starts over. Each item can be re-dispatched a few
times; past that, change the plan (edit, add, skip) rather than retrying.
Accept is for an item whose work is already there but whose agent never filed
a report (or filed failed): check it yourself, then accept. Never re-dispatch
an agent only to have it write the report. Accept does not use up a
re-dispatch, and releases the items that were waiting on it.
