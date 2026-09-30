"""MEM-14, RUNS-06, MEM-18: what the prompt teaches about memory. Curated notes
updated in place (not an append-only log), no notes about the harness itself,
the approval step named with where to do it, and a GUI map that matches the
navigation the operator actually has."""
import re

from backend import memory
from backend.db import get_db, init_db


def _section(title):
    body = memory.STATIC_BEHAVIOR.split(f"## {title}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_memory_discipline_says_curate_in_place_not_log():
    text = " ".join(_section("Memory discipline").split())
    assert "not a log" in text
    assert "mode=replace" in text and "Never append under a claim that is now false" in text
    assert "Merge duplicates" in text


def test_memory_discipline_keeps_harness_behaviour_out_of_memory():
    text = " ".join(_section("Memory discipline").split())
    assert "how Jav3's own tools or sandbox behave" in text
    assert "report_harness_fault" in text


def test_memory_discipline_names_the_approval_step_and_where():
    text = " ".join(_section("Memory discipline").split())
    assert "PENDING" in text and "Memory page" in text
    assert "never tell them a preference is in effect" in text
    assert "proposal" in text


def test_seed_soul_habit_matches():
    habit = memory.SEEDS["soul.md"].split("## Memory habit\n", 1)[1]
    flat = " ".join(habit.split())
    assert "in place" in flat and "instead of adding another" in flat
    assert "approval" in flat and "Memory page" in flat


def test_gui_map_matches_the_navigation():
    gui = " ".join(_section("The system around you").split())
    assert "Context (memory + secrets)" not in gui
    assert "Memory (where the operator approves the notes you save)" in gui
    for page in ("Work", "Agents", "Security", "VMs", "Tools", "Settings", "Schedules", "Shell"):
        assert page in gui, page


def test_the_gui_map_lists_the_pages_the_web_nav_has():
    # frontend/src/nav.jsx NAV_ITEMS labels, read from the source
    from backend.config import settings
    src = (settings.base_dir / "frontend" / "src" / "nav.jsx").read_text()
    labels = re.findall(r"label: '([^']+)'", src)
    gui = " ".join(_section("The system around you").split())
    assert labels and all(label in gui for label in labels), [l for l in labels if l not in gui]


async def test_the_assembled_prompt_carries_the_new_wording(tmp_env):
    await init_db()
    db = await get_db()
    try:
        prompt = await memory.assemble_system_prompt(db)
    finally:
        await db.close()
    assert "Memory page" in prompt and "not a log" in prompt
    assert "Context (memory + secrets)" not in prompt
