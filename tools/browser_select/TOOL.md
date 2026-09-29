---
name: browser_select
description: Choose an option in a native <select> dropdown (by id from browser_read_page) in a Jav3 browser tab, by its visible label or its value.
when_to_use: When browser_read_page lists a select line with its options and you need a different option. For custom dropdowns (role=combobox, listbox divs) click to open them and click the option instead.
enabled: true
section: browser
action: select
requires_browser: true
parameters:
  type: object
  properties:
    tab:
      type: integer
      description: Tab number.
    element:
      type: string
      description: Element id of the <select> from browser_read_page, e.g. "f0:7".
    label:
      type: string
      description: The option's visible text as listed (e.g. "United Kingdom"). Give label OR value.
    value:
      type: string
      description: The option's value attribute. Give label OR value.
    browser:
      type: string
      description: Which connected browser (name). Omit when only one is connected.
  required: [tab, element]
---
Needs a browser_read_page of this tab from this turn. Fires input and change like a real pick. The result ends with `changed: yes/no`. An id that has left the page returns "no longer on the page — browser_read_page again".
