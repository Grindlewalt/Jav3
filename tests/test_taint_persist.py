"""Taint is PERSISTED onto the note (not just the in-turn result): a memory
write in a turn that consumed untrusted content stamps `taint: untrusted`, which
survives append/replace and keeps the note out of binding context until the
operator promotes it. (B2 — the persisted half of the broker taint ledger.)"""
import importlib.util
from pathlib import Path

import pytest

from backend import memory, runtime
from backend.vm import broker

ROOT = Path(__file__).resolve().parent.parent


def _handler():
    spec = importlib.util.spec_from_file_location(
        "mw_handler", ROOT / "tools" / "memory_write" / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_untrusted_write_is_stamped_and_excluded(tmp_env):
    h = _handler()
    tok = runtime.write_taint.set("untrusted")
    try:
        await h.run("web-fact", "the sky is green", mode="replace")
    finally:
        runtime.write_taint.reset(tok)
    meta, _ = memory.parse_note((memory.notes_dir() / "web-fact.md").read_text())
    assert memory.note_taint(meta) == "untrusted"
    assert memory.note_trusted(meta) is False


async def test_clean_write_has_no_taint(tmp_env):
    h = _handler()
    await h.run("clean-fact", "2+2=4", mode="replace")
    meta, _ = memory.parse_note((memory.notes_dir() / "clean-fact.md").read_text())
    assert memory.note_taint(meta) == "trusted"


async def test_taint_survives_append(tmp_env):
    h = _handler()
    tok = runtime.write_taint.set("untrusted")
    try:
        await h.run("n", "first", mode="replace")
    finally:
        runtime.write_taint.reset(tok)
    # append later in a CLEAN turn — taint must not be dropped (the old bug)
    await h.run("n", "second", mode="append")
    meta, body = memory.parse_note((memory.notes_dir() / "n.md").read_text())
    assert memory.note_taint(meta) == "untrusted"
    assert "second" in body


async def test_promote_clears_taint(tmp_env):
    h = _handler()
    tok = runtime.write_taint.set("untrusted")
    try:
        await h.run("promote-me", "web claim", mode="replace")
    finally:
        runtime.write_taint.reset(tok)
    assert memory.promote_note("promote-me") is True
    meta, _ = memory.parse_note((memory.notes_dir() / "promote-me.md").read_text())
    assert memory.note_taint(meta) == "trusted"
    assert memory.note_trusted(meta) is True     # approved + untainted


async def test_broker_sets_taint_for_laundered_promotion(tmp_env, monkeypatch):
    """After a web_read, the broker sets write_taint during the memory_write
    dispatch so the handler stamps the note."""
    seen = {}

    async def fake_dispatch(name, args):
        if name == "memory_write":
            seen["taint"] = runtime.write_taint.get()
        return f"{name}-ok"

    monkeypatch.setattr(broker.registry, "dispatch", fake_dispatch)
    broker.register_turn(broker.TurnEnvelope(op_id="op-p"))
    try:
        await broker.broker_dispatch("op-p", "web_read", {"url": "http://x"})
        await broker.broker_dispatch("op-p", "memory_write", {"name": "f", "content": "c"})
        assert seen["taint"] == "untrusted"
    finally:
        broker.release_turn("op-p")


async def test_broker_no_taint_without_prior_untrusted(tmp_env, monkeypatch):
    seen = {}

    async def fake_dispatch(name, args):
        if name == "memory_write":
            seen["taint"] = runtime.write_taint.get()
        return f"{name}-ok"

    monkeypatch.setattr(broker.registry, "dispatch", fake_dispatch)
    broker.register_turn(broker.TurnEnvelope(op_id="op-c"))
    try:
        await broker.broker_dispatch("op-c", "read_file", {"path": "a"})
        await broker.broker_dispatch("op-c", "memory_write", {"name": "f", "content": "c"})
        assert seen["taint"] is None
    finally:
        broker.release_turn("op-c")
