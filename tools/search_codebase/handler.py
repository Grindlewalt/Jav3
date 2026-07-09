from backend.codeindex import index_stale, search_code
from backend.agent.tools.toolctx import require_project

CAP = 50


async def run(query: str, subdir: str | None = None, regex: bool = False) -> str:
    slug = await require_project()
    try:
        hits = search_code(slug, query, subdir=subdir, regex=regex,
                           max_results=CAP + 1, context=1)
    except ValueError as e:
        return f"error: {e}"
    if not hits:
        return (f"no matches for '{query}'. Try a shorter or broader term, drop subdir "
                "to search the whole project, or set regex=true for patterns.")
    truncated = len(hits) > CAP
    hits = hits[:CAP]

    # group by file and rank files by hit count, so "where does this concept
    # live" is answered by the ordering instead of 50 path-sorted stray lines
    by_file: dict[str, list[dict]] = {}
    for h in hits:
        by_file.setdefault(h["path"], []).append(h)

    out: list[str] = []
    stale = index_stale(slug)
    if stale:
        out.append(f"note: the codebase index is stale ({stale} files changed "
                   "since it was built) — re-run crawl_codebase if the overview matters")
    for path, hs in sorted(by_file.items(), key=lambda kv: -len(kv[1])):
        out.append(f"{path} ({len(hs)} match{'es' if len(hs) > 1 else ''})")
        # merge hits + context into one line map; a line that is both context
        # for one hit and a hit itself keeps the ':' match marker
        rows: dict[int, tuple[bool, str]] = {}
        for h in hs:
            no = h["line_no"]
            for off, ln in enumerate(h.get("before", []), start=no - len(h.get("before", []))):
                rows.setdefault(off, (False, ln))
            rows[no] = (True, h["line"])
            for off, ln in enumerate(h.get("after", []), start=no + 1):
                rows.setdefault(off, (False, ln))
        for no in sorted(rows):
            is_match, ln = rows[no]
            out.append(f"  {no}{':' if is_match else '-'} {ln}")
    if truncated:
        out.append(f"(showing first {CAP} matches — narrow the query)")
    return "\n".join(out)
