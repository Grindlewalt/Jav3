"""Guest-side `memory` shim. loop.py imports `standing_rules_tail` and injects
its result into the latest user turn. Memory lives on the host and never enters
the guest; the host computes the rules string and pushes it in the turn spec,
and this returns it."""
_rules = ""


def set_rules(rules: str | None) -> None:
    global _rules
    _rules = rules or ""


def standing_rules_tail() -> str:
    return _rules
