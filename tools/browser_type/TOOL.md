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
      type: string
      description: Field id from browser_read_page, e.g. "f0:12" (f0 is the top frame, f1+ are iframes).
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
Replaces the field's contents. Typing into a cross-origin iframe on a different site than the tab asks the operator to allow that site too.
