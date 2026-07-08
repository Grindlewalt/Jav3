---
name: run_command
description: Run a shell command inside the sandbox VM, in the active project's workspace. You have passwordless sudo. File changes made by the command are staged for operator approval.
when_to_use: Building, testing, installing packages (sudo apt/pip), inspecting — any shell work.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    command:
      type: string
    timeout:
      type: number
      description: Seconds before the command is killed (default 300).
  required: [command]
---
The workspace is re-synced from the host on every run — keep venvs and caches
OUTSIDE /workspace (e.g. in ~) if you want them to persist between runs.
