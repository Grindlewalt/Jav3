"""Guest-side `settings` shim. loop.py imports `from ..config import settings`
and the in-guest tools import `from backend.config import settings`; in the guest
both resolve here. The host pushes the live knob values in each turn spec
(apply()); the defaults mirror backend/config.py for a standalone run. No key, no
DB. `projects_dir`/`tools_dir` point at the pushed package layout (/opt/jarvis)."""
from pathlib import Path

# config.py is at <root>/backend/config.py; <root> (/opt/jarvis in the guest)
# is where the pushed workspace + tool handlers live alongside the backend pkg.
_BASE = Path(__file__).resolve().parent.parent


class _Settings:
    max_react_iterations = 60
    subagent_max_iterations = 12
    dead_end_force_answer = 8
    dead_end_error_streak = 4
    delegate_nudge_round = 12
    tool_result_max_chars = 12000
    tool_result_keep_recent = 2
    tool_result_evict_chars = 4000
    # where the pushed workspace unpacks and where the clean tool handlers live
    projects_dir = _BASE / "projects"
    tools_dir = _BASE / "tools"


settings = _Settings()


def apply(knobs: dict | None) -> None:
    for k, v in (knobs or {}).items():
        setattr(settings, k, v)
