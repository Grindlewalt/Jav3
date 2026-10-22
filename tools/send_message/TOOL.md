---
name: send_message
description: Send a message to another agent that is working right now, and to agents that are not (it waits in their inbox). Use it to coordinate instead of duplicating work.
when_to_use: When another running agent needs to know something you just learned, when you are about to touch a file or area another agent is working in, when you need an answer only another agent has, or to hand a peer a correction. Not for reporting to the operator — that is your final reply.
enabled: true
section: agents
action: send
parameters:
  type: object
  properties:
    to:
      type: array
      items:
        type: string
      description: Who gets it. One address as a plain string, or a list to send the same message to several. An address is an agent slug (e.g. "builder"), a conversation id (e.g. "42") or a plan item (e.g. "item:i3"). "items" stands for every item of your plan that is running or has not started. Pass "?" to list the turns running right now (a message sent with "?" is not sent but kept, so call again with the address and an empty message).
    message:
      type: string
      description: What to say. Self-contained — the recipient has NOT seen your conversation.
  required: [to, message]
---
Write it like a note to a colleague in another room: they have not seen your
conversation, so give the concrete paths, symbols and numbers, not "as I found
above". State plainly whether you need an answer or are just informing.

Addressing: a slug reaches whichever turn is running as that agent; a
conversation id reaches one exact thread, and every message you receive carries
its sender's id, so that is how you reply; item:<id> reaches the agent working
that checklist item of the current plan (if it has not started, or is blocked or
failed, the message is kept as a note in its brief for its next run). Send to "?" to see who is live.

Several at once: to=["item:i1", "builder"], or to="items" for the whole plan —
one call, one message stored per recipient (not one call per item). The result
lists who got it and who could not be reached.

This does not block and there is no way to wait for a reply inside this turn.
If the recipient is idle the message waits in its inbox and is delivered when
it next runs. Messages you receive appear on their own between your reasoning
rounds.
