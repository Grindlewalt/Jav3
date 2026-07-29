---
name: computer_status
description: List the operator's connected computers and what each can drive — monitors (for choosing a screen), audio outputs (for choosing where sound goes), any media player currently running, and which folders you may play from.
when_to_use: Before any computer_play or computer_open_link where the operator named a screen or a speaker ("play it on the TV", "put it through the desk speakers"), so you use the real device id instead of guessing. Also when they ask what you can control.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    client:
      type: string
      description: Which computer, if more than one is connected. Omit when there is only one.
---
Screens are indexed from 0 and audio devices have ids you pass verbatim to
`device`. If nothing is connected, say so plainly — the operator has to start
the client on the machine they want driven; you cannot start it for them.

The folders listed are the only places on disk you can reach. The operator
grants them on the Computer use tab; you have no way to add one.
