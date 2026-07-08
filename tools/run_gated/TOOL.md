---
name: run_gated
description: Run a shell command in the sandbox VM under full monitoring (M4 gate flow). The VM is wiped to a fresh golden image first; network egress is deny-by-default; DNS lookups, every exec, blocked connection attempts and a packet capture are collected into a gate report the operator reviews. File changes are staged for approval as usual.
when_to_use: Running untrusted or newly-written code, installing dependencies, or any run whose behavior the operator should be able to audit. Slower than run_command (fresh boot each time) — use run_command for quick iterative runs.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    command:
      type: string
      description: Shell command to run in /workspace/<project> inside the VM.
    timeout:
      type: number
      description: Seconds before the command is aborted (default 300).
    fresh:
      type: boolean
      description: Nuke the VM to a fresh image first (default true; false reuses current VM state and is faster but less clean).
  required: [command]
---
The gate report lands at `runs/gate-<id>/report.md` (staged). Tell the operator
to review it before approving staged changes from a gated run.
