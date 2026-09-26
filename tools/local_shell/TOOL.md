---
name: local_shell
description: Run a shell command on the operator's computer (a /local chat), in the local working directory. Waits for the operator's approval.
when_to_use: Running tests, builds, git or other commands the task needs on that machine.
enabled: true
requires_local: true
parameters:
  type: object
  properties:
    command:
      type: string
    timeout_seconds:
      type: integer
      description: 1..600 (default 120).
  required: [command]
---
Runs with the operator's own shell and permissions, not in the sandbox. The
operator approves each command (or a kind of command for the session); a
denial is an error, not a cue to retry. Output is capped and untrusted.
