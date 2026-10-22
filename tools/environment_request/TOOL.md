---
name: environment_request
description: Ask the operator for a whole new sandbox image for this project (a named variant built from an existing one, with several apt/pip/npm packages and optionally more memory). Filed for approval; nothing is built or changed now.
when_to_use: Only when this project needs SEVERAL packages or a bigger box on EVERY run. For one package use package_request; for a one-off need use `pip install`/`npm install` in run_code (wiped after each turn). Only this project moves to the new image.
enabled: true
section: project
action: environment
requires_settings: [vm_boxes_enabled]
parameters:
  type: object
  properties:
    name:
      type: string
      description: The new image name (lower-case letters, digits, `-` and `_`, e.g. `sim-gl`).
    from:
      type: string
      description: The existing image to build from (default `main`; `desktop` adds a screen and a browser).
    packages:
      type: array
      description: The packages, at most 30; plain names from the default registry only. A string like `apt ffmpeg`, `pip pygame==2.6.1` or `npm typescript@5.6.3` also works.
      items:
        type: object
        properties:
          manager:
            type: string
            enum: [apt, pip, npm]
          package:
            type: string
            description: The plain package name (e.g. `ffmpeg`, `requests`, `@scope/name`).
          version:
            type: string
            description: One exact version (optional).
        required: [manager, package]
    min_mem_mb:
      type: integer
      description: Optional memory floor for boxes on this image, 256 to 16384.
    reason:
      type: string
      description: What the environment is for and why it must persist.
  required: [name, packages, reason]
---
The host validates every package and builds its own install commands; URLs,
VCS references and registry flags are refused. Nothing is built until the
operator approves; approval creates the image and moves only this project onto
it (other projects keep their image), on a later box boot. Pending requests
are never auto-approved, and at most three may wait per project.
