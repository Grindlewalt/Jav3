---
name: web_read
description: Fetch one web page and return it as inert plain text (all scripts, styles and markup stripped). Internal/private addresses are refused.
when_to_use: When you need ONE page's full text verbatim (e.g. exact quotes, code). If you are surveying several sources, or don't need the page word-for-word, use read_and_summarize instead — full pages left in context are the main driver of runaway token cost.
enabled: true
read_only: true
parameters:
  type: object
  properties:
    url:
      type: string
      description: A full http(s) URL.
    extract:
      type: string
      description: Optional — what to pull from the page; returns a focused extraction instead of the raw page text.
  required: [url]
---
You never touch the raw internet — the host fetches and sanitizes the page for
you. If a URL was already fetched this session it won't be pulled again; choose
a different source to keep the gathered knowledge diverse.

API keys: write {{secret:NAME}} in the URL (e.g. ...?apiKey={{secret:NEWSAPI}})
and the host substitutes the real value at fetch time — but only toward the
hosts the operator bound that secret to. You never see the value; responses
that echo it come back scrubbed.
