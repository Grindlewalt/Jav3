---
name: run_code
description: Execute python code or a shell command inside the disposable sandbox VM and get exit code + stdout/stderr back. Runs against a copy of the active project's files; any files the run creates or changes are kept in the project.
when_to_use: Running or testing code you wrote, quick computations, transforms over project files (parse/convert/plot), or checking that a script actually works before proposing it. Prefer one script that does the whole job over many small runs.
enabled: true
section: files
core: true
parameters:
  type: object
  properties:
    code:
      type: string
      description: Python 3 source to execute (mutually exclusive with command).
    command:
      type: string
      description: A shell command line to execute (mutually exclusive with code).
    timeout_seconds:
      type: integer
      description: Kill the run after this many seconds (default 60, max 300).
---
The sandbox has no direct network and no secrets. With monitored egress on,
pip/npm/curl/git go through the host's proxy and only reach hosts the project's
Network policy allows (an undecided host is queued for the operator; a proxy
403 means refused or queued: report the exact hosts, do not probe the sandbox).
With egress off they fail by design; use web tools for anything remote, then
process it here. `node` and `npm` ARE installed (and `pytest`, in images built from the current
recipe), so JS and Python projects run and test in place (e.g. `node --test`,
`npm test`, `pytest`).

Servers and other background jobs: redirect their output (`cmd > /tmp/x.log
2>&1 &`) and kill them when you are done. A backgrounded process that keeps the
call's output open is detached after a couple of seconds and the call returns,
but it keeps running and holds its port for everyone on the shared box.

Your working directory is the project copy: read its files directly, and write
results as files — they sync back to the project at turn end. That sync keeps
EVERYTHING the run created under the project, so put throwaway scratch (probe
scripts, scratch logs, one-off experiments) under /tmp instead, where it is NOT
kept; node_modules, __pycache__, .pytest_cache and package caches are never kept. stdout/stderr are
truncated past ~6k chars — print what matters, write the rest to a file.
