import re
from datetime import date

from backend import memory, writes
from backend.db import get_db
from backend.memory import UNVERIFIED_MARK, read_project_md, refresh_all_projects
from backend.agent.tools.toolctx import require_project
from backend.runtime import write_taint

ENTRY_MAX = 500            # chars of one journal line

_JOURNAL_HEAD = re.compile(r"^## Journal[ \t]*$", re.M)
_NEXT_HEAD = re.compile(r"^## ", re.M)


def _add_line(md: str, line: str) -> str:
    """The entry goes at the end of the Journal section, wherever that is: at
    the end of the FILE it could land inside another section (a later
    '## Summary' would have carried it into the all-projects rollup)."""
    head = _JOURNAL_HEAD.search(md)
    if head is None:
        return md.rstrip() + "\n\n## Journal\n" + line + "\n"
    nxt = _NEXT_HEAD.search(md, head.end())
    end = nxt.start() if nxt else len(md)
    section = md[head.end():end].rstrip()
    tail = md[end:]
    return (md[:head.end()] + section + "\n" + line + "\n"
            + ("\n" + tail if tail else ""))


async def run(entry: str) -> str:
    slug = await require_project()
    # one plain line: an entry is not a place to open a new heading or section
    entry = memory.flat_line(entry, ENTRY_MAX)
    if not entry:
        return "error: the journal entry is empty."
    # a turn that had read untrusted content (the broker sets this for
    # journal_update, like memory_write) marks the line: project.md is loaded
    # whole into every future prompt, so the line stays in the file for the
    # operator to see but stays out of the prompt until they remove the tag
    tainted = bool(write_taint.get())
    tag = f" {UNVERIFIED_MARK}" if tainted else ""
    line = f"- {date.today().isoformat()}{tag}: {entry}"
    md = _add_line(read_project_md(slug), line)
    # project.md is re-injected into every future system prompt — it MUST cross
    # the apply_write chokepoint (secret refusal + advisory scan), not write_text
    try:
        await writes.apply_write(slug, "project.md", md.encode())
    except writes.SecretLeakError as e:
        return f"error: journal update refused — {e}"
    db = await get_db()
    try:
        await refresh_all_projects(db)
    finally:
        await db.close()
    if tainted:
        await memory.audit("journal_unverified", "info",
                           f"journal entry in '{slug}' marked {UNVERIFIED_MARK}: written "
                           "after untrusted content", {"project": slug})
        return (f"journal updated, marked {UNVERIFIED_MARK}: this turn read untrusted "
                "content, so the entry stays out of the prompt until the operator "
                "removes the tag in project.md. Tell them.")
    return "journal updated"
