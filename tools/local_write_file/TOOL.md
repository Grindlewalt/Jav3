---
name: local_write_file
description: Create or overwrite a file on the operator's computer (a /local chat). Waits for the operator's approval.
when_to_use: Creating a new local file, or replacing one wholesale. For a change to an existing file prefer local_edit_file.
enabled: true
requires_local: true
parameters:
  type: object
  properties:
    path:
      type: string
      description: Relative to the local working directory, or absolute. Parent directories are created.
    content:
      type: string
      description: The whole file.
  required: [path, content]
---
The operator sees the diff and approves or denies it. A denial is an error:
do not retry the same write; ask instead.
