---
name: research
description: Research a topic with a team of subagents. Decomposes it into angles, runs a focused research subagent per angle (coordinating so none scrape the same page), synthesizes their findings into one document, and stages it for approval.
when_to_use: When the operator asks you to research, investigate, or write up a topic that needs several web sources.
enabled: true
parameters:
  type: object
  properties:
    topic:
      type: string
      description: The topic or question to research.
    angles:
      type: integer
      description: How many angles to split it into (2-6, default 4).
  required: [topic]
---
This runs several subagents and many web fetches, so it takes a while and is
worth it for real research, not quick lookups (use web_search for those). The
finished document is written to research/ in the project (auto-approved by
default; otherwise staged for the operator). Read it with read_file if you need
the details; otherwise just tell the operator it's ready.
