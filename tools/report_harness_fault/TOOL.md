---
name: report_harness_fault
description: Report that the HARNESS itself misbehaved — a tool that errored on input you believed valid, or a documented capability that did not do what it says. Not for your own mistakes or a task going wrong.
when_to_use: When a tool rejects arguments that match its schema, a documented behaviour is missing (e.g. you cannot address a peer the system says exists), or a capability fails in a way you cannot route around by fixing your own call. First read the error and try the fix it suggests; report only a genuine harness fault, then route around it and keep working.
enabled: true
section: system
action: report_fault
read_only: true
parameters:
  type: object
  properties:
    what_i_tried:
      type: string
      description: The tool you called and what you were trying to do (e.g. 'send_message to a sibling plan item to coordinate a file').
    what_went_wrong:
      type: string
      description: The harness's fault, concretely — the error string or the wrong behaviour, verbatim where you can.
    what_i_expected:
      type: string
      description: What the harness should have done instead, per its own rules.
    severity:
      type: string
      enum: [low, medium, high]
      description: Default low. high = it blocked the task with no workaround.
  required: [what_i_tried, what_went_wrong]
---
This is a temporary diagnostic channel while the harness is being hardened. Use
it sparingly and factually: one report per distinct fault, not per retry. Quote
the exact error and the arguments you passed. After reporting, do NOT stop —
route around the fault (a different tool, a note, an assumption stated in one
line) and finish the task. The operator reviews these in the Review Center.
