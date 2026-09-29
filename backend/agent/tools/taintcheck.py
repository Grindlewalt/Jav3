"""Did an in-guest tool just touch a file a tainted turn wrote?

Pure, and shipped verbatim into the guest (vm/guest_pkg._COPY_MODULES): the guest
registry calls `touches` after read_file, search_codebase, crawl_codebase and
run_code, and when it names a path the turn was given as tainted
(backend/taintpaths.py, in the turn spec) it sends the gateway `taint_note` op
BEFORE the result reaches the model. Best effort by design: a run_code program
that opens a tainted file by a name it computes is not seen. The network side
of the same gap (a curl, a git clone) is closed host-side, at the egress proxy.
"""
import posixpath


def _norm(path) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    return posixpath.normpath(path.strip().replace("\\", "/")).lstrip("/")


def _names(text: str, tainted) -> str | None:
    for p in sorted(tainted, key=len, reverse=True):
        i = text.find(p)
        while i != -1:
            # a whole path segment on both sides: 'news-1.md' is not 'xnews-1.md'
            before = text[i - 1] if i else "/"
            after = text[i + len(p)] if i + len(p) < len(text) else "/"
            if before in "/ \t\n'\"`=(:" and (after.isspace() or after in "/'\"`):,;:"):
                return p
            i = text.find(p, i + 1)
    return None


def touches(name: str, args, result, tainted) -> str | None:
    """The tainted path `name(args) -> result` touched, or None."""
    if not tainted or not isinstance(args, dict):
        return None
    if name == "read_file":
        rel = _norm(args.get("path"))
        if not rel:
            return None
        for p in tainted:
            if rel == p or rel.endswith("/" + p):
                return p
        return None
    if name in ("search_codebase", "crawl_codebase"):
        return _names(result, tainted) if isinstance(result, str) else None
    if name == "run_code":
        code = " ".join(str(args.get(k) or "") for k in ("code", "command"))
        return _names(code, tainted)
    return None
