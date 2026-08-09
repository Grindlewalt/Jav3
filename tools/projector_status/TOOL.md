---
name: projector_status
description: What the projection mapper is doing — every surface and what it is showing, whether the projector output window is open, which displays are attached, and the state of the universe simulation.
when_to_use: When the operator asks what's on the wall, or when something did not appear and you need to know why. You do NOT need this before projector_show — that finds surfaces by name itself.
enabled: true
requires_settings: [mcp_projector_url, mcp_projector_token]
read_only: true
parameters:
  type: object
  properties: {}
---
This also confirms the projector app is reachable at all. If it is not, the app
is closed or the operator's machine is off — say that rather than retrying.

"output: closed" means nothing is being projected even though surfaces exist;
the editor window is showing them but the projector window is not open. That is
the most common reason the operator says they see nothing.
