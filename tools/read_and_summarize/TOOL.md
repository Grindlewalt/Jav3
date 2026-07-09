---
name: read_and_summarize
description: Fetch one or more web pages and return only a short bullet summary of each — the full page text is summarized inside the tool and never enters the conversation. Far cheaper than web_read when you are surveying many sources.
when_to_use: Reading several pages, or any page you don't need verbatim. Pass a list of urls to read them all in one call (one step, not one per page). Give a `focus` so each summary keeps only what matters. Prefer this over web_read whenever you are gathering information across multiple sources; use the research tool for a full multi-source report.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    urls:
      type: array
      items:
        type: string
      description: Full http(s) URLs to read and summarize (up to 8 per call).
    url:
      type: string
      description: A single URL, if you only have one.
    focus:
      type: string
      description: What you're looking for — each page is summarized against this.
---
Returns one "Source: <url>" block per page with 3-6 bullets. Because only the
summaries come back, you can read a dozen pages across a task without the
context (and token cost) snowballing.
