"""Memory frontmatter: written by yaml, read fail-closed (A0 hunt, 2026-09-29).

A description holding both quote kinds used to be written as a Python repr
(invalid YAML); parse_note then returned {} and the tainted agent note read as
operator-authored and TRUSTED, i.e. a binding rule in the system prompt."""
import importlib.util

import pytest

from backend import memory
from backend.config import settings


def _handler():
    spec = importlib.util.spec_from_file_location(
        "mw_handler", settings.tools_dir / "memory_write" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("desc", [
    "operator's \"rule\" note",
    "tab\there and 'both' \"quotes\"",
    "back\\slash: colon # hash",
    "it's a: [list-looking] {thing}",
])
def test_written_description_round_trips_and_stays_untrusted(desc):
    text = _handler()._with_frontmatter(desc, "body line", taint="untrusted")
    meta, body = memory.parse_note(text)
    assert meta["description"] == " ".join(desc.split())
    assert meta["source"] == "agent" and meta["approved"] is False
    assert memory.note_trusted(meta) is False
    assert body == "body line"


@pytest.mark.parametrize("text", [
    "---\nsource: agent\ndescription: 'it\\'s \"x\"'\n---\nbody\n",   # the old repr form
    "﻿---\nsource: agent\ndescription: [\n---\nbody\n",          # BOM + bad YAML
    "\n---\nsource: agent\ndescription: [\n---\nbody\n",              # leading blank line
    "---\n- just\n- a list\n---\nbody\n",                             # not a mapping
    "---\nsource: agent\nno closing fence\n",                         # never closed
])
def test_unreadable_frontmatter_fails_closed(text):
    meta, _ = memory.parse_note(text)
    assert memory.note_trusted(meta) is False
    assert memory.note_taint(meta) == "untrusted"


def test_plain_notes_and_good_frontmatter_unchanged():
    assert memory.parse_note("just a note\n") == ({}, "just a note")
    assert memory.note_trusted(memory.parse_note("just a note")[0]) is True
    meta, body = memory.parse_note("---\nsource: operator\n---\nhello\n")
    assert meta == {"source": "operator"} and body == "hello"
    assert memory.note_trusted(meta) is True
    # a leading BOM before good frontmatter no longer hides it
    meta, _ = memory.parse_note("﻿---\nsource: agent\napproved: false\n---\nx\n")
    assert meta["source"] == "agent" and memory.note_trusted(meta) is False


def test_promote_repairs_an_unreadable_note(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "notes_dir", lambda: tmp_path)
    (tmp_path / "n.md").write_text("---\nsource: agent\ndescription: [\n---\nkeep me\n")
    assert memory.promote_note("n") is True
    meta, body = memory.parse_note((tmp_path / "n.md").read_text())
    assert memory.note_trusted(meta) is True
    assert "_bad_frontmatter" not in meta and body == "keep me"
