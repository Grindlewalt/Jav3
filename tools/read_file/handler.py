from backend.config import settings
from backend.fsutil import find_file, list_tree
from backend.writes import resolve
from backend.agent.tools.toolctx import require_project


def _coerce(name: str, value):
    """(int|None, error) — accepts ints and digit strings, rejects the rest."""
    if value is None or isinstance(value, int):
        return value, None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip()), None
    return None, f"error: {name} must be an integer (got {value!r}) — e.g. offset=120, limit=80."


def _missing(slug: str, path: str) -> str:
    base = f"error: no such file '{path}' in project '{slug}'."
    name = path.rsplit("/", 1)[-1]
    near = [e["path"] for e in list_tree(settings.projects_dir / slug)
            if e["path"].rsplit("/", 1)[-1] == name and e["path"] != path]
    if near:
        # several files share the name — naming them all is the only useful
        # answer, since picking one would be a guess
        return f"{base} Did you mean: {', '.join(near[:6])}?"
    return f"{base} Use list_files to see what exists."


async def run(path: str, offset=None, limit=None) -> str:
    slug = await require_project()
    offset, err = _coerce("offset", offset)
    if err:
        return err
    limit, err = _coerce("limit", limit)
    if err:
        return err
    if offset is not None and offset < 1:
        return "error: offset is 1-based — pass offset=1 or higher."
    if limit is not None and limit < 1:
        return "error: limit must be at least 1 line."

    p = resolve(slug, path)
    note = ""
    if p is None:
        # "dashboards/weather.html" asked for as "weather.html" is the same
        # file, and answering "no such file" spends a whole turn re-deriving a
        # path this tool can just find. Only when it is unambiguous.
        found, _ = find_file(settings.projects_dir / slug, path)
        if found is None:
            return _missing(slug, path)
        note = f"(read '{found}' — the only file matching '{path}')\n"
        path, p = found, resolve(slug, found)
        if p is None:
            return _missing(slug, path)
    try:
        text = p.read_text()
    except UnicodeDecodeError:
        return f"error: {path} is binary ({p.stat().st_size} bytes)"
    if not text:
        return note + "(empty file)"

    cap = settings.tool_result_max_chars
    lines = text.splitlines()
    total = len(lines)

    if offset is None and limit is None:
        # throw, don't truncate: a tiny error beats a cap-sized dump in context
        if len(text) > cap:
            return (f"error: {path} is {total} lines / {len(text):,} chars — too big to "
                    "return whole. Re-call read_file with offset (1-based start line) and "
                    "limit (line count) to read a slice, or use search_codebase to find "
                    "the right region.")
        return note + text

    start = offset or 1
    if start > total:
        return f"error: {path} has only {total} lines (you asked for offset {start})."
    end = min(total, start - 1 + (limit if limit is not None else total))
    # no per-line number prefixes: edit_file needs the exact text to match
    body = "\n".join(lines[start - 1:end])
    if len(body) > cap:
        body = body[:cap] + f"\n...(slice truncated at {cap:,} chars — use a smaller limit)"
    return f"{note}(lines {start}-{end} of {total} — {path})\n{body}"
