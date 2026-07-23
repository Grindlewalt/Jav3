---
name: create_agent
description: Define a new named agent (system prompt + roster entry) that you can then run with spawn_agent or propose a schedule for — or, with update=true, rewrite an existing agent's prompt. For a one-off subtask an existing agent covers, just spawn_agent it instead.
when_to_use: The operator asks for a new kind of agent ("make a news agent"), wants an existing agent's behavior changed ("make the news agent shorter" -> update=true), or a recurring task needs a role no agent in your roster covers. Check the roster first — never duplicate an existing agent.
enabled: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Short human name, e.g. "News scout". The slug is derived from it.
    description:
      type: string
      description: One line for the roster — what this agent is for.
    prompt:
      type: string
      description: "The agent's full system prompt (150-300 words), second person (\"You are...\"), structured with these markdown headings in order — # Context (who it is, runs headless), # Objective (exact scope, what done looks like, what it must NOT do), # Style (method, tools), # Tone, # Audience, # Response (exact output format)."
    update:
      type: boolean
      description: Set true to rewrite an EXISTING agent (same name/slug) with this prompt. Read the current definition first and carry forward what should stay — this replaces the whole prompt, it does not merge.
  required: [name, prompt]
---
If the agent needs an API key, NEVER put a real value in the prompt — reference
{{secret:NAME}} instead (available names are in your context; the operator adds
keys and binds their web hosts in the Secrets panel). Placeholders resolve only
in web_read URLs on the secret's bound hosts.

The new agent gets every tool and context section by default (the operator
trims exclusions in the Agents tab) and works under the same write scans
as you. It is spawnable immediately — tell the operator you created it so they
can review the prompt.
