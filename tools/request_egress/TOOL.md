---
name: request_egress
description: Ask the operator to allow one outbound network destination (host + port) for this project's sandbox runs. The sandbox blocks all egress by default; use this when your in-VM code needs a specific host (e.g. pypi.org:443 for pip, github.com:443 for git clone).
when_to_use: When a command you ran in the sandbox failed because it couldn't reach the network, and you know the host and port it needs. Explain briefly why. Nothing opens until the operator approves — file the request and continue with what you can; the destination works on the next run once approved.
enabled: true
requires_project: true
parameters:
  type: object
  properties:
    host:
      type: string
      description: The hostname or IP the connection needs, e.g. "pypi.org".
    port:
      type: integer
      description: The port, e.g. 443.
    reason:
      type: string
      description: One line on why this destination is needed.
  required: [host, port]
---
