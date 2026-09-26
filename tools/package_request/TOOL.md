---
name: package_request
description: Ask the operator to add a package permanently to this project's sandbox image (apt, pip or npm). Filed for approval; nothing is installed now.
when_to_use: Only when a tool is needed on EVERY run and a per-run `pip install`/`npm install` in run_code is not enough (it is wiped after each turn). Never for one-off use.
enabled: true
requires_settings: [vm_boxes_enabled]
parameters:
  type: object
  properties:
    manager:
      type: string
      enum: [apt, pip, npm]
      description: The package manager.
    package:
      type: string
      description: The plain package name from the default registry (e.g. `ffmpeg`, `requests`, `@scope/name`). No URLs, paths, git refs or flags.
    version:
      type: string
      description: One exact version (optional; the dry-run pins the current one for the operator to see).
    install_command:
      type: string
      description: The command you would have run. Stored for the operator to read; never executed.
    reason:
      type: string
      description: What the package is for and why it must persist.
  required: [manager, package, install_command, reason]
---
The host builds its own install command from the validated name and version;
index/registry flags, URLs and VCS references are refused. An approved package
goes into the image variant this project's profile uses, so every project on
that variant gets it; the approval builds a new image version, which the
project's next box boot picks up. Pending requests are never auto-approved.
