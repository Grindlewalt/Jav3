---
name: workspace_panel
description: Arrange the active project's workspace board — add/remove panels, open a file in an editor/renderer panel, or tile everything neatly. Changes appear live in any open board and persist for the next visit.
when_to_use: When the operator asks you to set up, open, show or clean up panels on a project's board ("open the journal next to the chat", "show me sim.py", "tidy this up"), or when you want to surface something you just built (a rendered html file, the run panel) without making them dig for it.
enabled: true
parameters:
  type: object
  properties:
    action:
      type: string
      enum: [add, remove, open_file, tile]
      description: add a panel type · remove all panels of a type · open_file (editor or renderer picked by extension) · tile the whole board into neat rows.
    panel:
      type: string
      description: Panel type for add/remove — chat, journal, editor, renderer, organizer, run, todos, git, board, context, agent, research, review, network, secrets.
    path:
      type: string
      description: Project-relative file for open_file (or to pre-load into an added editor/renderer panel).
  required: [action]
---
Works on the ACTIVE project's board. open_file picks the renderer for
html/pdf/images and the editor for everything else. add places the new panel to
the right of the board; follow with action=tile if it should flow into the
grid. remove takes a panel type and removes every panel of that type.
