"""The turn spec is a dict with no schema, handed across a process boundary.

`backend/vm/guest_turn.py` builds it; the modules under `guest/` unpack it by
hand, key by key. Nothing checks that those two lists agree, and since M4e
deleted the host-side loop there is no second path whose behaviour would
diverge and give the miss away — a key the host writes and the guest never
reads is simply ignored, in production, silently.

That has now happened twice:

  * M4e (2026-08-02) — `model_name` / `base_url` / `rewrite_rules`. The local
    voice tier was configured on the Pi and answering from DeepSeek anyway.
    Caught by a billing row, not a test.
  * `inject_rules` (2026-08-10) — the guest defaulted it to True, so the small
    local model spent a week reading the standing rules as something the
    operator had just said, and replying to them.

Both were invisible to every other test in the suite, because a dropped knob
produces a turn that works — just not the turn that was asked for. This is the
structural check: the host's spec keys must be a subset of the guest's reads.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The anchors below are load-bearing. If guest_turn is refactored so its spec
# literal is no longer a dict assigned to a name `spec`, the scan silently finds
# nothing and this test would pass while checking nothing at all — so we assert
# the scan found the keys we know are there before trusting its verdict.
_ANCHORS = {"system_prompt", "history", "tool_specs"}


def _keys_written() -> set[str]:
    """Every string key `guest_turn` puts into the turn spec."""
    tree = ast.parse((ROOT / "backend/vm/guest_turn.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "guest_turn")
    keys: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        # spec = {"a": ..., "b": ...}
        if (isinstance(node.value, ast.Dict)
                and any(isinstance(t, ast.Name) and t.id == "spec"
                        for t in node.targets)):
            keys |= {k.value for k in node.value.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        # spec["a"] = ...   (the conditional workspace tar)
        for t in node.targets:
            if (isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Name) and t.value.id == "spec"
                    and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)):
                keys.add(t.slice.value)
    return keys


def _keys_read() -> set[str]:
    """Every string key anything under guest/ pulls back out of the spec.

    Not just server.py: `turnctx.enter(spec, slug)` consumes five of them, so a
    scan of the entry point alone would report false misses."""
    keys: set[str] = set()
    for path in sorted((ROOT / "guest").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            # spec.get("a")
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "spec"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
            # spec["a"]
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "spec"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                keys.add(node.slice.value)
    return keys


def test_every_spec_key_the_host_writes_is_read_by_the_guest():
    written = _keys_written()
    read = _keys_read()

    assert _ANCHORS <= written, (
        "the spec scan found nothing recognisable, so it is no longer checking "
        "anything — teach it guest_turn's current shape rather than deleting "
        f"it. Found: {sorted(written)}")

    missing = written - read
    assert not missing, (
        "guest_turn puts these keys in the turn spec and nothing under guest/ "
        "ever reads them, so they are dropped on the floor in production with "
        f"no error and no test failure anywhere else: {sorted(missing)}. "
        "Either read the key in the guest or stop sending it.")


def test_the_parity_check_can_actually_fail():
    """A guard whose failure path never runs is a guard nobody has tested.

    The check above is only worth having if a missing key really does trip it,
    so this pins the assertion's direction: extra reads in the guest are fine
    (`mode` is read and never sent by this caller), a missing read is not."""
    written, read = _keys_written(), _keys_read()
    assert "inject_rules" in written and "inject_rules" in read   # the 08-10 bug
    assert (written | {"a_knob_nobody_reads"}) - read == {"a_knob_nobody_reads"}
    assert "mode" in read and "mode" not in written
