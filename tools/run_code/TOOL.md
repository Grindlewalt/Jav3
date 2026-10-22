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
      description: Kill after this many seconds (default 60, max 300); a `timeout N` in the command does not raise it, and a kill loses output piped to tail/head/grep.
---
No secrets. Network goes through the host's proxy to hosts the project's Network
policy allows (a 403 = refused or queued: report the hosts, don't probe). `node`,
`npm` and (current images) `pytest` are installed.

Packages: per-run `apt-get`/`pip`/`npm install` works when the policy allows the
host (apt: deb.debian.org), gone after the turn; package_request (if the profile
allows) is for one every run needs.

`command` runs under /bin/sh (dash: no arrays, PIPESTATUS, pipefail or [[ ]]; use
`bash -c`). The interpreter is `python3`. The cwd is already the project: no
`cd "$(pwd)" &&`.

Background jobs: redirect output (`cmd > /tmp/x.log 2>&1 &`) and kill what you
start; an unredirected one is detached but keeps its port.

Files it changes sync back at turn end; scratch goes in /tmp (RAM, ~350 MB, not
kept; installs go in the project's .venv). node_modules, .venv and caches are
never kept. Output truncates past ~6k chars.
