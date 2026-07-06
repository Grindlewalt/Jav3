"""Deterministic output hygiene for mechanical formatting preferences.

Some preferences are formatting habits a fast model won't reliably obey from
an instruction alone (it keeps emitting em dashes however sternly told not to).
For those we ALSO enforce the rule on the output stream. Rules are derived from
the operator's standing memory, so this stays memory-driven: state the
preference and it's enforced; remove it and enforcement stops.
"""
from ..memory import memory_block

EM_DASH = "—"


def output_replacements() -> dict[str, str]:
    """Mechanical substitutions to apply to model output, read from memory."""
    mem = memory_block().lower()
    repl: dict[str, str] = {}
    if "em dash" in mem or "em-dash" in mem or "emdash" in mem:
        # collapse "a — b" and "a—b" to a comma both read cleanly
        repl[" " + EM_DASH + " "] = ", "
        repl[EM_DASH] = ", "
    return repl


def scrub(text: str, repl: dict[str, str]) -> str:
    for a, b in repl.items():
        text = text.replace(a, b)
    return text
