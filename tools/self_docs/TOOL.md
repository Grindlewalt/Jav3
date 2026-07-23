---
name: self_docs
description: Your own technical manual — how Jarvis works (architecture, memory, secrets, egress, GUI, tools, multi-agent). No args = section list; section=... returns that section.
when_to_use: Before explaining how you work, debugging your own behavior (a refused write, a secret that won't inject, a missing panel), or answering the operator's questions about the system. Read the relevant section instead of guessing.
enabled: true
parameters:
  type: object
  properties:
    section:
      type: string
      description: Section name from the list (e.g. secrets, gui, egress and security). Omit for the section list; "all" for the whole manual.
---
The manual ships with the codebase (docs/SELF.md) so it is always in sync with
the deployed build. Prefer one targeted section over "all" — the whole manual
is long.
