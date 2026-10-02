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
      description: Kill the run after this many seconds (default 60, max 300). Set it to cover the whole command; a `timeout 280` inside the command does not raise it. A killed run loses output piped into tail/head/grep.
---
No direct network, no secrets. With monitored egress on, pip/npm/apt-get/curl go
through the host's proxy and reach only hosts the project's Network policy allows
(a 403 = refused or queued: report the exact hosts, do not probe); with it off they
fail. `node`, `npm` and (current images) `pytest` are installed.

Packages: a per-run `apt-get`/`pip`/`npm install` here works when the policy allows
the host (apt: deb.debian.org) and is gone after the turn. package_request is for a
package EVERY run needs, and only where the profile allows requests; if refused,
install per run here.

`command` runs under /bin/sh (dash: no arrays, PIPESTATUS, pipefail or [[ ]]; use
`bash -c`). The interpreter is `python3`. The cwd is already the project: no
`cd "$(pwd)" &&`.

Background jobs: redirect output (`cmd > /tmp/x.log 2>&1 &`) and kill what you
start; an unredirected one is detached but keeps its port.

Files the run creates or changes sync back at turn end; throwaway scratch goes under
/tmp (RAM-backed, ~350 MB, not kept: no venvs or big installs; use .venv in the
project). node_modules, .venv, caches and __pycache__ are never kept. Output
truncates past ~6k chars: write the rest to a file.
