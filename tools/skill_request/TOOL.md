---
name: skill_request
description: Ask the operator to install an existing published skill (a GitHub repo folder or a ClawHub slug). Filed for approval; nothing is fetched or installed now.
when_to_use: Only for an existing published skill you have found, named as an https git URL (optionally `#sub/dir`) or `clawhub:<slug>`. Never to install packages or programs (that is package_request), and never to write a new skill yourself.
enabled: true
section: skills
parameters:
  type: object
  properties:
    source:
      type: string
      description: "An https:// git URL, optionally followed by #sub/dir, or `clawhub:<slug>[@version]`. No local paths, credentials or query strings."
    name:
      type: string
      description: Optional tool name to install it under (letters, digits, _ and -).
    reason:
      type: string
      description: What the skill is for and why you need it.
  required: [source, reason]
---
Nothing is contacted when you file the request. The operator fetches the skill
themselves, reads it, and only then installs and grants it. It appears in your
tools as an installed skill in a later turn, if approved.
