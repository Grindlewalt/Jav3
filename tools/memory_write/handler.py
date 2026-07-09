import re

from backend.memory import notes_dir, parse_note


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError("bad note name")
    return slug


def _with_frontmatter(description: str | None, body: str) -> str:
    text = body.rstrip() + "\n"
    if description:
        # single-line YAML value; a stray colon/quote must not break parsing
        desc = " ".join(description.split())
        text = f"---\ndescription: {desc!r}\n---\n{text}"
    return text


async def run(name: str, content: str, mode: str = "append",
              description: str | None = None) -> str:
    notes = notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / f"{_safe_name(name)}.md"
    if mode == "delete":
        if not path.exists():
            return (f"error: no note named '{name}' to delete — "
                    "list notes with memory_read first")
        path.unlink()
        return f"memory note '{path.stem}' deleted"
    if mode == "replace" or not path.exists():
        path.write_text(_with_frontmatter(description, content))
        return f"memory note '{path.stem}' written"
    # append: keep (or update) the existing frontmatter, never duplicate it
    meta, body = parse_note(path.read_text())
    desc = description or meta.get("description")
    path.write_text(_with_frontmatter(desc, body + "\n\n" + content.strip()))
    return f"appended to memory note '{path.stem}'"
