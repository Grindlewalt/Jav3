---
name: send_message
description: Send a message to another agent that is working right now, and to agents that are not (it waits in their inbox). Use it to coordinate instead of duplicating work.
when_to_use: When another running agent needs to know something you just learned, when you are about to touch a file or area another agent is working in, when you need an answer only another agent has, or to hand a peer a correction. Not for reporting to the operator — that is your final reply.
enabled: true
parameters:
  type: object
  properties:
    to:
      type: string
      description: An agent slug (e.g. "builder") or a conversation id (e.g. "42"). Pass "?" to list the turns running right now.
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
its sender's id, so that is how you reply. Send to "?" to see who is live.

This does not block and there is no way to wait for a reply inside this turn.
If the recipient is idle the message waits in its inbox and is delivered when
it next runs. Messages you receive appear on their own between your reasoning
rounds.
