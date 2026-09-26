---
name: service_request
description: Request a long-running service (a server, worker or bot) for the project that keeps running after this turn. This only FILES the request; nothing starts until the operator approves it. The operator reviews a content-hashed snapshot of the named files, the exact argv, env, ports and egress hosts.
when_to_use: Only when something genuinely has to persist between turns (a dev server the operator wants to keep, a bot, a scheduled worker). Not for one-off runs (use run_code). Write and test the code first; the snapshot is taken from the project's files as they are now.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Short id, lowercase letters/digits/'-' (e.g. "api"). A new request with the same name replaces the approved one after review (shown as a diff).
    description:
      type: string
      description: What the service does, one or two sentences.
    command:
      type: array
      items:
        type: string
      description: The argv to run, NOT a shell string (e.g. ["python3", "server.py", "--port", "8080"]). Runs from workdir inside the snapshot.
    workdir:
      type: string
      description: Directory inside the snapshot to run from (relative to the project root; default ".").
    files:
      type: array
      items:
        type: string
      description: Project paths or globs to snapshot (e.g. ["server.py", "app/**"]). Only these files exist in the service's box, read-only.
    ports:
      type: array
      items:
        type: object
        properties:
          port:
            type: integer
            description: TCP port the service listens on (1024-65535).
          protocol:
            type: string
            enum: [tcp]
          purpose:
            type: string
            description: What the port is for.
          expose:
            type: string
            enum: [none, host, lan]
            description: none = inside the box only; host = ask the operator to relay it to the Jav3 host (loopback); lan = ask for the LAN (dedicated address only).
        required: [port, purpose]
    restart:
      type: string
      enum: ["no", "on-failure", "always"]
    egress_hosts:
      type: array
      items:
        type: string
      description: Hostnames the service may reach (deny-by-default; nothing else is reachable).
    env:
      type: object
      additionalProperties:
        type: string
      description: Environment variables (UPPER_SNAKE_CASE). Secrets are NOT allowed here, neither values nor {{secret:NAME}} placeholders.
    reason:
      type: string
      description: Why this needs to keep running (the operator reads this).
  required: [name, command, files, reason]
---
Files a service request. The host snapshots the listed files from the
project's live files (not from your sandbox), hashes them, and refuses the
request if any file or env value carries a secret. The operator then chooses
where it runs and which ports (if any) are exposed.

Once approved, the service runs in its own service box: a fresh root disk every
boot, its code read-only at /opt/svc/<id>, and ONE persistent writable
directory, `$SRV` (also `$STATE_DIRECTORY`). Its outbound traffic goes only to
the approved `egress_hosts`, through the monitored proxy. It has no access to
the model, the tools, the workspace or any secret.

Check it with service_status; read its output with service_logs.
