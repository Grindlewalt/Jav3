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
pip/npm/curl go through the host's proxy and reach only hosts the project's
Network policy allows (a proxy 403 = refused or queued: report the exact hosts,
do not probe the sandbox); with it off they fail by design. `node`, `npm` and
(in current images) `pytest` are installed: run and test in place.

`command` runs under /bin/sh, which is dash: no arrays, `${PIPESTATUS[0]}`,
`pipefail`, `[[ ]]` or `<( )`; wrap bash-isms as `bash -c '...'`. The interpreter
is `python3`; there is no `python`. The working directory is already the project,
so do not prefix commands with `cd "$(pwd)" &&`.

Background jobs: redirect output (`cmd > /tmp/x.log 2>&1 &`) and kill what you
start; an unredirected one is detached after ~2 s but keeps running, and keeps
its port for everyone on the shared box.

Your working directory is the project copy: read its files directly, and write
results as files — they sync back to the project at turn end. That sync keeps
EVERYTHING the run created, so put small throwaway scratch under /tmp, where it
is NOT kept (it is RAM-backed and small, ~350 MB: no venvs or big installs
there; use .venv in the project). node_modules, .venv, __pycache__,
.pytest_cache and package caches are never kept. stdout/stderr are truncated
past ~6k chars: print what matters, write the rest to a file.
