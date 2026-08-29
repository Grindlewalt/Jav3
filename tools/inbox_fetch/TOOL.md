---
name: inbox_fetch
description: Internal. Drains the running turn's inbox of messages other agents addressed to it. The ReAct loop calls this between iterations; the model is never offered it.
enabled: false
parameters:
  type: object
  properties: {}
---
Not a tool the model calls, and `enabled: false` keeps it out of every tool
spec — it is here because a tool folder is the one thing a guest can invoke
across the vsock boundary, and `broker_dispatch` is where op_id pinning lives.
The loop dispatches it by name (backend/agent/loop.py:_drain_inbox); the guest's
registry brokers it to the host like any other non-in-guest tool.

It takes no arguments on purpose. The recipient is the turn's own conversation,
resolved from the host-side envelope, so calling it can only ever drain your own
inbox — there is no argument through which to read somebody else's.

Do not set `enabled: true`. The loop already drains every round; offering it as
well would only let the model spend a round asking for messages it was going to
be handed anyway.
