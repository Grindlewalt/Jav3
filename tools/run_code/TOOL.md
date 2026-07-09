---
name: run_code
description: Run a Python snippet inside the sandbox VM with the active project's files as the working directory. Files it writes are staged for approval.
when_to_use: Quick calculations, checks or experiments that don't warrant a saved script.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    code:
      type: string
      description: Python source to execute.
    timeout:
      type: number
  required: [code]
---
The VM has NO internet access except operator-allowlisted destinations —
network calls from code WILL fail. Gather web data with web_search / web_read /
read_and_summarize (host-side) and feed it into the code as inputs. Only call
run_code to actually execute something; reasoning belongs in your reply text.
