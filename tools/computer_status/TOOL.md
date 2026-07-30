---
name: computer_status
description: List the operator's connected computers and what each can drive — monitors (for choosing a screen), audio outputs (for choosing where sound goes), any media player currently running, and which folders you may play from.
when_to_use: Only when the operator asks what you can control, or after an action came back saying a device name did not match. Do NOT call this before acting: the action tools resolve a device name themselves and their error lists the options, so a preflight check just costs a round trip and tokens.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    client:
      type: string
      description: Which computer, if more than one is connected. Omit when there is only one.
---
Screens are indexed from 0. There are TWO audio lists and they are not
interchangeable: "mixer outputs" are what computer_volume's `device` accepts,
"playback outputs" are what computer_play's `device` accepts. Pass either
verbatim; do not translate between them. If nothing is connected, say so plainly — the operator has to start
the client on the machine they want driven; you cannot start it for them.

The folders listed are the only places on disk you can reach. The operator
grants them on the Computer use tab; you have no way to add one.
