"""Argument checking shared by the host and guest tool dispatchers.

A wrong argument used to cost a whole round: "bad arguments … unexpected keyword
argument 'min_elements'" and the model had to guess again. Now:

* a READ-ONLY tool runs anyway with the unknown arguments dropped, and the
  result says what was ignored and what the tool takes (nothing it could do
  can be changed by an argument it never saw);
* a tool that CHANGES things still refuses (dropping an argument there could
  change what happens), but the error lists the valid and required parameters
  and the closest names, so the retry is right the first time;
* a crash inside a handler reports the error and where it happened, not a
  traceback the model cannot act on.

Pure: copied verbatim into the guest package (backend/vm/guest_pkg.py).
"""
import difflib
import inspect
import traceback


# names that claim WHO is calling: identity is host-derived, never an argument.
# Passing one is refused outright (loudly), even on a read-only tool.
IDENTITY_ARGS = frozenset({"from", "sender", "sender_cid", "sender_id", "conversation_id",
                           "op_id", "op_token", "actor", "as_user", "user_id"})


def _params(handler) -> tuple[list[str], list[str], bool]:
    """(all parameter names, required ones, takes **kwargs)."""
    names, required, var_kw = [], [], False
    for p in inspect.signature(handler).parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            var_kw = True
        elif p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            names.append(p.name)
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
    return names, required, var_kw


def _suggest(bad: str, names: list[str]) -> str:
    close = difflib.get_close_matches(bad, names, n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def prepare(name: str, handler, args: dict, *, read_only: bool) -> tuple[dict, str, str | None]:
    """-> (args to call with, note to append to the result, error or None)."""
    names, required, var_kw = _params(handler)
    args = dict(args or {})
    unknown = [] if var_kw else [k for k in args if k not in names]
    missing = [r for r in required if r not in args]
    takes = ", ".join(f"{n}{'' if n in required else '?'}" for n in names) or "no arguments"
    if missing:
        return args, "", (f"error: {name} needs {', '.join(missing)} — it takes: {takes} "
                          "(? = optional). Call it again with those.")
    if not unknown:
        return args, "", None
    detail = "; ".join(f"'{k}'{_suggest(k, names)}" for k in unknown)
    if not read_only or IDENTITY_ARGS.intersection(unknown):
        return args, "", (f"error: {name} has no parameter {detail} — it takes: {takes} "
                          "(? = optional). Nothing ran; call it again without those.")
    for k in unknown:
        args.pop(k)
    return args, (f"\n\n[note: {name} ignored {detail} — not a parameter of this tool. "
                  f"It takes: {takes}.]"), None


def crash_message(name: str, e: BaseException) -> str:
    """One line the model can act on, plus where it happened (for a harness
    fault report) — not a traceback."""
    where = ""
    frames = traceback.extract_tb(e.__traceback__)
    if frames:
        f = frames[-1]
        where = f" (at {f.filename.rsplit('/', 2)[-2:][0]}/{f.filename.rsplit('/', 1)[-1]}:{f.lineno})"
    return (f"error: {name} failed: {type(e).__name__}: {e}{where}. If your arguments were "
            "right, this is a harness fault: report it with report_harness_fault and try "
            "another way; otherwise adjust the arguments.")
