---
name: desk_shell
description: Run a shell command on the operator's connected computer and return its output.
when_to_use: When a command is the direct way to do it on that computer (list files, check a process, run a script) rather than clicking.
enabled: true
requires_desk: true
parameters:
  type: object
  properties:
    cmd:
      type: string
    cwd:
      type: string
      description: Working directory; defaults to the home directory.
    timeout:
      type: integer
      description: Seconds, 1..120 (default 60).
    computer:
      type: string
      description: Which connected computer (name). Omit when only one is connected.
  required: [cmd]
---
Commands on the computer's allowlist run at once; anything else waits (up to
60 s) for the operator to allow it. Output is capped and untrusted.
