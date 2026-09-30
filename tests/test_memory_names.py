"""MEM-11: a note the operator named by hand ('My Ideas', 'ideas_v2', 'v1.2-plan',
'Homelab') can be read, written and deleted by the name the prompt's index shows,
and the agent does not get a second file for it. Very long names are an error
string, not an OSError."""
import importlib.util

import pytest

from backend import memory
from backend.config import settings

NAMES = ["My Ideas", "ideas_v2", "v1.2-plan", "Homelab"]


def _tool(tool):
    spec = importlib.util.spec_from_file_location(
        f"names_{tool}", settings.tools_dir / tool / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def notes(tmp_env):
    d = memory.notes_dir()
    d.mkdir(parents=True, exist_ok=True)
    for n in NAMES:
        (d / f"{n}.md").write_text(f"body of {n}\n")
    return d


@pytest.mark.parametrize("asked,stem", [
    ("My Ideas", "My Ideas"),        # exact
    ("my ideas", "My Ideas"),        # case
    ("my-ideas", "My Ideas"),        # the slug the tools used to insist on
    ("homelab", "Homelab"),
    ("ideas-v2", "ideas_v2"),
    ("ideas_v2", "ideas_v2"),
    ("V1.2-Plan", "v1.2-plan"),
])
def test_resolve_note(notes, asked, stem):
    assert memory.resolve_note(asked) == stem


def test_resolve_note_unknown_and_junk(notes):
    assert memory.resolve_note("nope") is None
    assert memory.resolve_note("!!!") is None
    assert memory.resolve_note("x" * 400) is None


async def test_memory_read_opens_a_note_by_any_of_its_names(notes):
    read = _tool("memory_read").run
    for asked in ("My Ideas", "my ideas", "my-ideas", "ideas-v2", "Homelab"):
        assert "body of" in await read(asked), asked
    out = await read("nothing-here")
    assert out.startswith("error: no note named") and "My Ideas" in out
    assert (await read("!!!")).startswith("error:")            # no ValueError
    assert (await read("x" * 400)).startswith("error:")        # no OSError


async def test_memory_write_uses_the_existing_file_not_a_second_one(notes):
    write = _tool("memory_write").run
    out = await write("ideas-v2", "one more", mode="replace")
    assert "ideas_v2" in out                       # told which note it hit
    assert not (notes / "ideas-v2.md").exists()
    # the operator's note is binding, so the change is a proposal for THAT note
    assert memory.proposal_path("ideas_v2", notes).is_file()
    assert (notes / "ideas_v2.md").read_text() == "body of ideas_v2\n"
    assert sorted(p.stem for p in notes.glob("*.md")) == sorted(NAMES)


async def test_memory_delete_finds_a_hand_named_note(notes):
    out = await _tool("memory_write").run("My Ideas", "", mode="delete")
    assert "deleted" in out and not (notes / "My Ideas.md").exists()
    assert [t["name"] for t in memory.list_trash()] == ["My Ideas"]
    assert memory.restore_trash(memory.list_trash()[0]["id"]) == "My Ideas"


async def test_a_new_note_gets_a_short_slug(tmp_env):
    write = _tool("memory_write").run
    out = await write("A " + "very " * 60 + "long name", "x", mode="replace")
    stems = [p.stem for p in memory.notes_dir().glob("*.md")]
    assert len(stems) == 1 and len(stems[0]) <= memory.NOTE_NAME_MAX
    assert "saved" in out
    assert (await write("!!!", "x")).startswith("error: bad note name")
    assert (await write("x" * 400, "y", mode="replace")).startswith("memory note")
