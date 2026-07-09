---
name: run_command
description: Run a shell command inside the sandbox VM, in the active project's workspace. You have passwordless sudo. File changes made by the command are staged for operator approval.
when_to_use: Building, testing, running project code, inspecting — any shell work on files you already have.
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
The VM has NO internet access except operator-allowlisted destinations: curl,
pip/apt installs and API calls WILL fail unless the operator has allowlisted
that host. To gather web data, use web_search / web_read / read_and_summarize
(host-side tools) and feed the results into your code as inputs. If a run
needs a new outbound destination, say so — the operator can allowlist it.
