"""Argument checking shared by the host and guest tool dispatchers.

A wrong argument used to cost a whole round: "bad arguments … unexpected keyword
argument 'min_elements'" and the model had to guess again. Now:

* a READ-ONLY tool runs anyway with the unknown arguments dropped, and the
  result says what was ignored and what the tool takes (nothing it could do
  can be changed by an argument it never saw);
* a tool that CHANGES things still refuses (dropping an argument there could
  change what happens), but the error lists the valid and required parameters
  and the closest names, so the retry is right the first time;
* a misspelled REQUIRED argument that is one clear match ('replacement' for
  'replace') is taken as the one it means, with a note; when it is not clear the
  error names the key the model sent;
* an argument of the wrong TYPE (a dict for text, 'four' for a count) is
  coerced when the meaning is plain ('3' -> 3, 'false' -> False, null for an
  optional argument -> its default) and refused with one plain sentence when it
  is not, checked against the handler's own annotations, so no handler has to
  repeat it and none of them crashes on it;
* a crash inside a handler reports the error and where it happened, not a
  traceback the model cannot act on; the crashes that are really the model's
  path being wrong read as that, not as a harness fault.

Pure: copied verbatim into the guest package (backend/vm/guest_pkg.py).
"""
import difflib
import errno
import inspect
import json
import math
import re
import traceback
import types
import typing


# names that claim WHO is calling: identity is host-derived, never an argument.
# Passing one is refused outright (loudly), even on a read-only tool.
IDENTITY_ARGS = frozenset({"from", "sender", "sender_cid", "sender_id", "conversation_id",
                           "op_id", "op_token", "actor", "as_user", "user_id"})

# an unknown key becomes a required one only when it is this close to it
REMAP_CUTOFF = 0.75


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


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _remap(args: dict, names: list[str], missing: list[str], unknown: list[str]) -> list[tuple[str, str]]:
    """Take an unknown key as the required argument it clearly misspells:
    [(key, argument)], with `args`, `missing` and `unknown` updated in place.
    Only when that key's best match among ALL the parameters is the missing one,
    it is close, and no other unknown key claims the same argument — otherwise
    it is a guess, and the error says what was seen instead."""
    done = []
    for want in list(missing):
        claims = [k for k in unknown if k not in IDENTITY_ARGS
                  and _ratio(k, want) >= REMAP_CUTOFF
                  and max(names, key=lambda n: _ratio(k, n)) == want]
        if len(claims) == 1:
            k = claims[0]
            args[want] = args.pop(k)
            unknown.remove(k)
            missing.remove(want)
            done.append((k, want))
    return done


# ---- argument types, from the handler's own annotations ----

_WORDS = {str: "text", int: "a whole number", float: "a number", bool: "true or false",
          list: "a list", dict: "an object"}
_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _kinds(annotation) -> tuple[tuple[type, ...], bool] | None:
    """(the types an annotation allows, whether None is allowed) for one made
    only of str / int / float / bool / list / dict (and None); None for anything
    else, which is the handler's own business."""
    if annotation is inspect.Parameter.empty or isinstance(annotation, str):
        return None
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = typing.get_args(annotation)
    else:
        members = (annotation,)
    kinds, nullable = [], False
    for m in members:
        if m is type(None):
            nullable = True
            continue
        base = typing.get_origin(m) or m
        if base not in _WORDS:
            return None
        kinds.append(base)
    return (tuple(kinds), nullable) if kinds else None


def _coerce(value, kinds: tuple[type, ...]):
    """(value as one of `kinds`, True), or (value, False) when its meaning is not plain."""
    t = type(value)
    if t in kinds:
        return value, True
    if t is int and float in kinds:
        return float(value), True
    if t is int and bool in kinds and value in (0, 1):
        return bool(value), True
    if t is float and int in kinds and value.is_integer():
        return int(value), True
    if t in (int, float) and str in kinds:          # not bool: True is not text
        return str(value), True
    if isinstance(value, str):
        s = value.strip()
        if int in kinds and re.fullmatch(r"[+-]?\d+", s):
            return int(s), True
        if float in kinds:
            try:
                f = float(s)
            except ValueError:
                f = None
            if f is not None and math.isfinite(f):
                return f, True
        if bool in kinds and s.lower() in _TRUE | _FALSE:
            return s.lower() in _TRUE, True
        for want, open_ in ((list, "["), (dict, "{")):
            if want in kinds and s.startswith(open_):
                try:
                    got = json.loads(s)
                except ValueError:
                    continue
                if isinstance(got, want):
                    return got, True
    return value, False


def _got(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    r = repr(value)
    return r if len(r) <= 40 else r[:37] + "...'"


def _words(kinds: tuple[type, ...]) -> str:
    w = [_WORDS[k] for k in kinds]
    return w[0] if len(w) == 1 else ", ".join(w[:-1]) + " or " + w[-1]


def _typed(handler, args: dict) -> list[str]:
    """Coerce `args` in place to the handler's annotated types; the problems
    that cannot be coerced, one phrase each. A None for a parameter that has a
    default is 'not given' (the model writing null for an optional argument),
    dropped so the default applies."""
    problems = []
    for p in inspect.signature(handler).parameters.values():
        if p.name not in args:
            continue
        parsed = _kinds(p.annotation)
        if parsed is None:
            continue
        kinds, nullable = parsed
        value = args[p.name]
        if value is None:
            if nullable:
                continue
            if p.default is not inspect.Parameter.empty:
                del args[p.name]
            else:
                problems.append(f"{p.name} is required (got null)")
            continue
        got, ok = _coerce(value, kinds)
        if ok:
            args[p.name] = got
        else:
            problems.append(f"{p.name} must be {_words(kinds)} (got {_got(value)})")
    return problems


def prepare(name: str, handler, args: dict, *, read_only: bool) -> tuple[dict, str, str | None]:
    """-> (args to call with, note to append to the result, error or None)."""
    names, required, var_kw = _params(handler)
    args = dict(args) if isinstance(args, dict) else {}
    unknown = [] if var_kw else [k for k in args if k not in names]
    missing = [r for r in required if r not in args]
    takes = ", ".join(f"{n}{'' if n in required else '?'}" for n in names) or "no arguments"
    took = _remap(args, names, missing, unknown) if missing and unknown else []
    notes = [f"{name} took '{k}' as '{a}'." for k, a in took]
    if missing:
        seen = ""
        if unknown:
            seen = (" You passed "
                    + "; ".join(f"'{k}'{_suggest(k, missing) or _suggest(k, names)}"
                                for k in unknown)
                    + ", which is not a parameter of this tool.")
        return args, "", (f"error: {name} needs {', '.join(missing)} — it takes: {takes} "
                          f"(? = optional).{seen} Call it again with those.")
    if unknown:
        detail = "; ".join(f"'{k}'{_suggest(k, names)}" for k in unknown)
        if not read_only or IDENTITY_ARGS.intersection(unknown):
            return args, "", (f"error: {name} has no parameter {detail} — it takes: {takes} "
                              "(? = optional). Nothing ran; call it again without those.")
        for k in unknown:
            args.pop(k)
        notes.append(f"{name} ignored {detail} — not a parameter of this tool. "
                     f"It takes: {takes}.")
    problems = _typed(handler, args)
    if problems:
        return args, "", (f"error: {name}: {'; '.join(problems)}. "
                          "Nothing ran; call it again with those fixed.")
    return args, (f"\n\n[note: {' '.join(notes)}]" if notes else ""), None


# exceptions that are the path or the arguments being wrong, not the harness:
# (type, what to say). Matched by name so this module imports nothing beyond
# the standard library (HTTPException comes from fastapi via fsutil.safe_join).
def _user_fault(e: BaseException) -> str | None:
    detail = getattr(e, "detail", None)
    status = getattr(e, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and isinstance(detail, str):
        if "escapes" in detail:
            return ("that path is outside the project — use a path relative to the "
                    "project root, without '..' or a leading '/'")
        return detail
    if isinstance(e, (FileExistsError, NotADirectoryError)):
        return ("a folder in that path is really a file — pick a path whose parent "
                "folders do not exist as files")
    if isinstance(e, IsADirectoryError):
        return "that path is a folder, not a file"
    if isinstance(e, ValueError) and "embedded null" in str(e):
        return "the path contains a NUL character"
    if isinstance(e, OSError) and e.errno == errno.ENAMETOOLONG:
        return "that path is too long"
    return None


def crash_message(name: str, e: BaseException) -> str:
    """One line the model can act on, plus where it happened (for a harness
    fault report) — not a traceback."""
    if getattr(e, "for_model", False):
        # an error written for the model (toolctx.NoProjectError): its message
        # is the whole answer, and it is not a harness fault
        return f"error: {name}: {str(e).rstrip('.')}."
    fault = _user_fault(e)
    if fault:
        return f"error: {name}: {fault}."
    where = ""
    frames = traceback.extract_tb(e.__traceback__)
    if frames:
        f = frames[-1]
        where = f" (at {f.filename.rsplit('/', 2)[-2:][0]}/{f.filename.rsplit('/', 1)[-1]}:{f.lineno})"
    return (f"error: {name} failed: {type(e).__name__}: {e}{where}. If your arguments were "
            "right, this is a harness fault: report it with report_harness_fault and try "
            "another way; otherwise adjust the arguments.")
