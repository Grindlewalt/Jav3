---
name: organize_project
description: Propose and apply an organization scheme for a project's files, guided by per-directory marks (.about.md).
when_to_use: When the operator asks to organize, tidy, or restructure a project's files ("Jarvis, organize this project").
enabled: false
parameters:
  type: object
  properties:
    slug:
      type: string
      description: project slug to organize
  required:
    - slug
---

TODO (lands with the tool layer — not granted yet):

- Read the project's dirs and their `.about.md` marks plus the file list
  (`/api/projects/{slug}/dirs`, `/files` — or direct fs access as a tool).
- Propose a scheme: new dirs (with marks) + file moves.
- Surface the proposal in the Organizer panel: the board shows the proposed
  layout with an approve / change / deny bar (green applies the moves, yellow
  = operator tells Jarvis what to change, red reverts). Operator can still
  drag files around inside the proposal before approving.
- For files Jarvis can't place: in chat, a small dialog navigated with
  1/2/3 or up/down + enter; free-text always allowed ("make a misc") — the
  next ReAct iteration creates the dir and moves the file. In the workspace
  file tab, the same controls appear as an integrated popup.
