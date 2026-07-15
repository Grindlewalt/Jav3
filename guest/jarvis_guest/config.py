"""Guest-side `settings` shim. loop.py imports `from ..config import settings`;
in the guest that resolves here. The host pushes the live knob values in each
turn spec (apply()); the defaults mirror backend/config.py for a standalone run.
Only the knobs the loop actually reads live here — no key, no DB, no paths that
matter until M3's in-guest tools."""


class _Settings:
    max_react_iterations = 60
    subagent_max_iterations = 12
    dead_end_force_answer = 8
    dead_end_error_streak = 4
    delegate_nudge_round = 12
    tool_result_max_chars = 12000
    tool_result_keep_recent = 2
    tool_result_evict_chars = 4000
    # filled in M3 (in-guest tools operate on the pushed workspace)
    projects_dir = None
    tools_dir = None


settings = _Settings()


def apply(knobs: dict | None) -> None:
    for k, v in (knobs or {}).items():
        setattr(settings, k, v)
