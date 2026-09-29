---
name: ask_user
description: Ask the operator 1-6 questions, each with 2-5 short answer options, and wait for the answer. The operator can also type a free answer or skip.
when_to_use: When you need the operator's input or a clarifying decision you cannot make yourself (which of several reasonable approaches, a missing requirement, a preference). Not for things you can find out with your own tools, and not to ask permission for routine work.
enabled: true
section: system
core: true
parameters:
  type: object
  properties:
    questions:
      type: array
      description: One entry per question, asked in order.
      items:
        type: object
        properties:
          question:
            type: string
            description: The question, one or two sentences.
          options:
            type: array
            description: 2-5 short, distinct answer labels. Do not add an "other" option; the operator can always type their own.
            items:
              type: string
          multi_select:
            type: boolean
            description: true lets the operator pick several options (default false).
        required: [question, options]
  required: [questions]
---
Blocks until the operator answers (up to an hour). The result lists what they
picked and anything they typed. If they skip, proceed on your best judgement
and say what you assumed; do not immediately ask the same thing again.
