"""Tier-4 taint tracking at the broker: untrusted-tool output is marked, and a
memory promotion made after untrusted content was consumed is quarantined on
the result the model sees. The static memory rule (agent notes approved:false)
is the primary block; this is the runtime half."""
from backend.vm import broker


def _reg(op_id="op-t"):
    broker.register_turn(broker.TurnEnvelope(op_id=op_id, web_session="ws"))


async def _dispatch(monkeypatch, calls):
    """Drive a sequence of (name, args) through broker_dispatch with a fake
    registry that echoes a canned result per tool. Returns the result strings."""
    async def fake_dispatch(name, args):
        return f"{name}-ok"
    monkeypatch.setattr(broker.registry, "dispatch", fake_dispatch)
    out = []
    for name, args in calls:
        r = await broker.broker_dispatch("op-t", name, args)
        out.append(r)
    return out


async def test_untrusted_tool_marks_the_op(tmp_env, monkeypatch):
    _reg()
    try:
        assert broker.op_tainted("op-t") is False
        await _dispatch(monkeypatch, [("web_read", {"url": "http://x"})])
        assert broker.op_tainted("op-t") is True
    finally:
        broker.release_turn("op-t")


async def test_trusted_only_op_stays_clean(tmp_env, monkeypatch):
    _reg()
    try:
        await _dispatch(monkeypatch, [("read_file", {"path": "a"}),
                                      ("list_files", {})])
        assert broker.op_tainted("op-t") is False
    finally:
        broker.release_turn("op-t")


async def test_promotion_after_untrusted_is_quarantined(tmp_env, monkeypatch):
    """web_read then memory_write: the note write comes back with the taint
    quarantine note appended for the model to see."""
    _reg()
    try:
        res = await _dispatch(monkeypatch, [
            ("web_read", {"url": "http://evil"}),
            ("memory_write", {"name": "fact", "content": "the sky is green"})])
        assert res[1]["taint"] == "trusted"        # memory_write itself isn't untrusted
        assert "quarantined" in res[1]["result"]
        assert "memory_write-ok" in res[1]["result"]
    finally:
        broker.release_turn("op-t")


async def test_promotion_without_prior_untrusted_is_clean(tmp_env, monkeypatch):
    """memory_write with no untrusted content consumed first: no quarantine note
    — a normal note the operator will still approve, but not flagged as laundered."""
    _reg()
    try:
        res = await _dispatch(monkeypatch, [
            ("read_file", {"path": "a"}),
            ("memory_write", {"name": "fact", "content": "2+2=4"})])
        assert "quarantined" not in res[1]["result"]
    finally:
        broker.release_turn("op-t")


async def test_promotion_order_matters(tmp_env, monkeypatch):
    """memory_write BEFORE any untrusted read is clean; a later web_read taints
    the op but can't retroactively quarantine the earlier write."""
    _reg()
    try:
        res = await _dispatch(monkeypatch, [
            ("memory_write", {"name": "a", "content": "x"}),
            ("web_read", {"url": "http://y"})])
        assert "quarantined" not in res[0]["result"]
        assert broker.op_tainted("op-t") is True
    finally:
        broker.release_turn("op-t")


async def test_failed_promotion_not_annotated(tmp_env, monkeypatch):
    _reg()
    try:
        async def fake_dispatch(name, args):
            return "error: bad note name" if name == "memory_write" else f"{name}-ok"
        monkeypatch.setattr(broker.registry, "dispatch", fake_dispatch)
        await broker.broker_dispatch("op-t", "web_read", {"url": "http://x"})
        r = await broker.broker_dispatch("op-t", "memory_write", {"name": ""})
        assert r["result"].startswith("error:")
        assert "quarantined" not in r["result"]
    finally:
        broker.release_turn("op-t")


async def test_release_clears_taint(tmp_env, monkeypatch):
    _reg()
    await _dispatch(monkeypatch, [("web_read", {"url": "http://x"})])
    assert broker.op_tainted("op-t") is True
    broker.release_turn("op-t")
    assert broker.op_tainted("op-t") is False      # a reused op_id starts clean


async def test_quarantine_note_names_what_tainted_the_turn(tmp_env, monkeypatch):
    """The trial's note: a turn tainted only by desk tools was labelled
    "(web/research)". The label now follows the recorded source kind; the
    quarantine itself is unchanged."""
    _reg()
    try:
        res = await _dispatch(monkeypatch, [
            ("desk_screenshot", {"computer": "grant-mac-desk"}),
            ("memory_write", {"name": "state", "content": "TextEdit works"})])
        note = res[1]["result"]
        assert 'already read the screen of "grant-mac-desk" (desk). It is quarantined' \
            in note
        assert "web" not in note
        assert broker.taint_sources("op-t") == [("desk", "grant-mac-desk")]
    finally:
        broker.release_turn("op-t")
    assert broker.taint_sources("op-t") == []
    _reg()
    try:
        res = await _dispatch(monkeypatch, [
            ("web_read", {"url": "http://x"}),
            ("local_read_file", {"path": "README"}),
            ("desk_click", {}),                      # one desk: no name in the args
            ("memory_write", {"name": "n", "content": "c"})])
        assert ("already read a web page, read files or command output from the "
                "operator's machine (local) and read a computer's screen (desk). "
                "It is quarantined") in res[3]["result"]
    finally:
        broker.release_turn("op-t")


async def test_desk_act_names_the_desk_before_the_broker_does(tmp_env, monkeypatch):
    """desk.act marks the turn with the desk's real name (the model may omit
    `computer`); the broker's name-less mark afterwards does not erase it."""
    _reg()
    try:
        broker.mark_tainted("op-t", "desk", "grant-mac-desk")
        broker._note_source("op-t", "desk", None)
        broker.mark_tainted("op-t", "browser")
        broker.mark_tainted("op-t", "desk", 'x"\n' * 40)      # junk detail ignored
        assert broker.taint_sources("op-t") == [("desk", "grant-mac-desk"),
                                                ("browser", None)]
        broker.mark_tainted("op-t", "nonsense")               # unknown kind: no label
        assert len(broker.taint_sources("op-t")) == 2
    finally:
        broker.release_turn("op-t")


async def test_unlabelled_taint_keeps_the_generic_note(tmp_env, monkeypatch):
    """A taint with no recorded kind (the guest's taint_note) still quarantines."""
    _reg()
    try:
        broker.mark_tainted("op-t")
        res = await _dispatch(monkeypatch, [("memory_write", {"name": "n", "content": "c"})])
        assert "already consumed untrusted external content. It is quarantined" \
            in res[0]["result"]
    finally:
        broker.release_turn("op-t")


async def test_shell_taint_is_recorded_as_its_own_source(tmp_env):
    _reg()
    try:
        broker.mark_tainted("op-sh", "desk_shell", "grant-mac-desk")
        assert broker.taint_sources("op-sh") == [("desk_shell", "grant-mac-desk")]
        assert broker.op_tainted("op-sh")
    finally:
        broker.release_turn("op-sh")
