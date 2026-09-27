---
name: browser_type
description: Type text into a field (by number from browser_read_page) in a Jav3 browser tab.
when_to_use: To fill a search box or form field you saw in the latest browser_read_page of that tab.
enabled: true
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    element:
      type: integer
      description: Field number from browser_read_page.
    text:
      type: string
      description: At most 2000 characters. Stored secrets are refused.
    submit:
      type: boolean
      description: Press Enter / submit the form afterwards.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, element, text]
---
Replaces the field's contents.
