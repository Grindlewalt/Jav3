---
name: run_code
description: Run a Python snippet inside the sandbox VM with the active project's files as the working directory. Files it writes are staged for approval.
when_to_use: Quick calculations, checks or experiments that don't warrant a saved script.
enabled: true
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
